"""Train on one public dataset SWE task via PPO with frozen-SFT KL anchor.

Requires the ROLL backend config to enable the reference cluster so
compute_logprobs(base_model=...) can route to the frozen reference model.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import torch

import tinker
from tinker_cookbook.rl.rollouts import do_single_rollout
from tinker_cookbook.rl.types import Trajectory
from tinker_cookbook.tinker_backend_cookbook.completer import RuntimePromptCompleter
from tinker_cookbook.tinker_backend_cookbook.env import RemoteSandboxEnv
from tinker_cookbook.tinker_backend_cookbook.train_swe_bench import evaluate_after_training
from tinker_cookbook.tinker_backend_cookbook.server_job_marker import emit_server_job_marker
from tinker_cookbook.tinker_backend_cookbook.tasks import (
    add_task_args,
    load_one_task,
    task_name,
    write_task_manifest,
)
from tinker_cookbook.tinker_backend_cookbook.trajectory_format import (
    format_structured_trajectory,
)


def build_training_examples(
    trajectory: Trajectory,
) -> tuple[list[tinker.types.Datum], list[torch.Tensor], list[torch.Tensor]]:
    """Identical to train_swe_bench.build_training_examples.

    Returns examples, old_logprobs, advantages — all parallel lists.
    """
    total_return = sum(t.reward for t in trajectory.transitions)

    examples: list[tinker.types.Datum] = []
    old_logprobs_list: list[torch.Tensor] = []
    advantages_list: list[torch.Tensor] = []

    for transition in trajectory.transitions:
        if not transition.ac.tokens:
            continue
        examples.append(
            tinker.types.Datum(
                model_input=transition.ob,
                loss_fn_inputs={"target_tokens": transition.ac.tokens},
            )
        )
        old_logprobs_list.append(
            torch.tensor(transition.ac.logprobs, dtype=torch.float32)
        )
        advantages_list.append(
            torch.full((len(transition.ac.tokens),), total_return, dtype=torch.float32)
        )

    return examples, old_logprobs_list, advantages_list


async def compute_ref_logprobs_for_responses(
    ref_client: tinker.SamplingClient,
    examples: list[tinker.types.Datum],
) -> list[torch.Tensor]:
    """Score the response tokens of each example under the frozen reference.

    For each Datum (prompt, response):
      1. Concatenate prompt_tokens + response_tokens into a single ModelInput
      2. ref_client.compute_logprobs(full_input) returns per-position logprobs
         of length len(prompt)+len(response). Position 0 is None (BOS).
      3. Slice [len(prompt):] to keep only the response-token logprobs.

    The resulting tensor aligns 1:1 with target_tokens, same shape as the
    new_logprobs the loss_fn receives from forward_backward_custom.
    """
    tasks = []
    prompt_lens: list[int] = []
    for ex in examples:
        prompt_tokens = ex.model_input.to_ints()
        response_tokens = ex.loss_fn_inputs["target_tokens"].tolist()
        full_input = tinker.types.ModelInput.from_ints(prompt_tokens + response_tokens)
        prompt_lens.append(len(prompt_tokens))
        tasks.append(asyncio.wrap_future(ref_client.compute_logprobs(full_input)))

    all_results = await asyncio.gather(*tasks)

    out: list[torch.Tensor] = []
    for full_lp, prompt_len in zip(all_results, prompt_lens, strict=True):
        resp_lp = full_lp[prompt_len:]
        # Defensive: convert any None to 0.0; should not occur past position 0,
        # but the SDK contract allows None anywhere if the server couldn't score
        # that position (e.g. partial logprob support).
        resp_lp_floats = [float(lp) if lp is not None else 0.0 for lp in resp_lp]
        out.append(torch.tensor(resp_lp_floats, dtype=torch.float32))
    return out


def make_ppo_loss_with_kl(
    old_logprobs: list[torch.Tensor],
    ref_logprobs: list[torch.Tensor],
    advantages: list[torch.Tensor],
    clip_epsilon: float,
    kl_beta: float,
):
    """Clipped PPO surrogate + Schulman k3 KL penalty against frozen reference.

    loss = -mean(min(r*A, clip(r,1±ε)*A)) + β * mean(kl_k3)
        r = exp(new_lp - old_lp)
        kl_k3 = exp(new_lp - ref_lp) - 1 - (new_lp - ref_lp)   # always ≥ 0

    Schulman k3 is the low-variance, always-non-negative KL estimator. Its
    gradient through new_lp is (ratio_ref - 1), which pulls the policy logprob
    toward the reference on tokens where they disagree.
    """

    def ppo_loss(
        _data: list[tinker.types.Datum],
        logprobs_list: list[torch.Tensor],
    ) -> tuple[torch.Tensor, dict[str, float]]:
        device = logprobs_list[0].device
        dtype = logprobs_list[0].dtype
        total_loss = torch.zeros((), dtype=dtype, device=device)
        total_ratio = 0.0
        total_clipfrac = 0.0
        total_kl = 0.0
        total_kl_ref_log_ratio = 0.0
        total_tokens = 0

        for new_lp, old_lp_cpu, ref_lp_cpu, adv_cpu in zip(
            logprobs_list, old_logprobs, ref_logprobs, advantages, strict=True
        ):
            old_lp = old_lp_cpu.to(device=device, dtype=dtype)
            ref_lp = ref_lp_cpu.to(device=device, dtype=dtype)
            adv = adv_cpu.to(device=device, dtype=dtype)

            # --- PPO clipped surrogate ---
            ratio = (new_lp - old_lp).exp()
            surr1 = ratio * adv
            surr2 = ratio.clamp(1.0 - clip_epsilon, 1.0 + clip_epsilon) * adv
            total_loss = total_loss - torch.minimum(surr1, surr2).sum()

            # --- KL penalty against ref (Schulman k3) ---
            log_ratio_ref = new_lp - ref_lp
            ratio_ref = log_ratio_ref.exp()
            kl_per_token = ratio_ref - 1.0 - log_ratio_ref
            total_loss = total_loss + kl_beta * kl_per_token.sum()

            # --- metrics ---
            clipped = (
                (ratio < 1.0 - clip_epsilon) | (ratio > 1.0 + clip_epsilon)
            ).float()
            total_ratio += float(ratio.detach().sum())
            total_clipfrac += float(clipped.detach().sum())
            total_kl += float(kl_per_token.detach().sum())
            total_kl_ref_log_ratio += float(log_ratio_ref.detach().sum())
            total_tokens += ratio.numel()

        if total_tokens == 0:
            raise ValueError("ppo_loss: zero tokens")

        total_loss = total_loss / total_tokens
        metrics = {
            "trainer/ppo_kl_loss": float(total_loss.detach()),
            "trainer/ratio_mean": total_ratio / total_tokens,
            "trainer/clipfrac": total_clipfrac / total_tokens,
            "trainer/kl_per_token": total_kl / total_tokens,
            "trainer/kl_ref_log_ratio_mean": total_kl_ref_log_ratio / total_tokens,
            "trainer/kl_beta": kl_beta,
        }
        return total_loss, metrics

    return ppo_loss


async def run_one_task_and_train(
    task: tinker.TaskDescriptor,
    runtime: Any,
    training_client: tinker.TrainingClient,
    sampling_client: tinker.SamplingClient,
    ref_sampling_client: tinker.SamplingClient,
    results_dir: Path,
    max_turns: int = 40,
    max_tokens: int = 8192,
    temperature: float = 0.7,
    learning_rate: float = 3e-6,
    weight_decay: float = 0.0,
    clip_epsilon: float = 0.2,
    kl_beta: float = 0.05,
) -> dict:
    task_label = task_name(task)
    print(f"Task: {task_label}")

    # --- Rollout (identical to eval_swe_bench.py / train_swe_bench.py) ---
    env = RemoteSandboxEnv(
        task=task,
        runtime=runtime,
        max_turns=max_turns,
    )
    policy = RuntimePromptCompleter(
        sampling_client=sampling_client,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    started_at = time.monotonic()
    trajectory = await do_single_rollout(policy, env)
    rollout_elapsed = time.monotonic() - started_at

    reward = sum(t.reward for t in trajectory.transitions)
    turns_used = len(trajectory.transitions)
    print(f"Rollout done: turns={turns_used}, reward={reward:.3f}, elapsed={rollout_elapsed:.1f}s")

    (results_dir / "trajectory.txt").write_text(
        format_structured_trajectory(trajectory, only_last_transition=False)
    )

    # --- Training step ---
    examples, old_logprobs, advantages = build_training_examples(trajectory)

    if not examples:
        print("No training examples (all transitions had empty tokens), skipping train step.")
        return {
            "task_name": task_label,
            "reward": reward,
            "turns_used": turns_used,
            "rollout_elapsed": round(rollout_elapsed, 1),
            "train_skipped": True,
        }

    # NEW: score response tokens under the frozen reference model.
    # Routing: ref_sampling_client was built with base_model=... (no checkpoint),
    # so ROLL sees model_id="" and dispatches to its reference cluster.
    print(f"Computing ref logprobs for {len(examples)} transition(s)...")
    ref_started = time.monotonic()
    ref_logprobs = await compute_ref_logprobs_for_responses(ref_sampling_client, examples)
    ref_elapsed = time.monotonic() - ref_started
    print(
        f"  ref logprobs done in {ref_elapsed:.1f}s, "
        f"shapes={[tuple(lp.shape) for lp in ref_logprobs]}"
    )

    print(f"Training on {len(examples)} transitions (kl_beta={kl_beta})...")
    train_started = time.monotonic()

    loss_fn = make_ppo_loss_with_kl(
        old_logprobs, ref_logprobs, advantages, clip_epsilon, kl_beta
    )
    fwdbwd_future = training_client.forward_backward_custom(examples, loss_fn)
    fwdbwd_result = await fwdbwd_future
    print(f"forward_backward done: {fwdbwd_result.metrics}")

    optim_future = training_client.optim_step(
        tinker.types.AdamParams(learning_rate=learning_rate, weight_decay=weight_decay)
    )
    optim_result = await optim_future
    print(f"optim_step done: {optim_result.metrics}")

    new_sampling_client = await training_client.publish_to_sampler()
    train_elapsed = time.monotonic() - train_started
    print(f"Weights published to sampler. Train step elapsed={train_elapsed:.1f}s")

    result = {
        "task_name": task_label,
        "reward": reward,
        "turns_used": turns_used,
        "rollout_elapsed": round(rollout_elapsed, 1),
        "ref_logprobs_elapsed": round(ref_elapsed, 1),
        "train_elapsed": round(train_elapsed, 1),
        "fwdbwd_metrics": fwdbwd_result.metrics,
        "optim_metrics": optim_result.metrics or {},
        "published_sampling_client": new_sampling_client is not None,
    }
    (results_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Result: {result}")
    return result


async def main_async() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    add_task_args(parser)
    parser.add_argument("--api-key", default="tml-dummy")
    parser.add_argument("--runtime-type", default="ROLL")
    parser.add_argument("--config-path", default=str(Path(__file__).resolve().parents[1] / "config" / "tinker_backend_cookbook" / "roll_train_runtime.yaml"))
    parser.add_argument("--model-name", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument(
        "--ref-model-name",
        default=None,
        help="Base model name used as the frozen KL reference. Defaults to --model-name. "
             "ROLL ignores this value and uses its yaml 'pretrain' field for the "
             "reference cluster weights — keep them aligned to avoid surprise.",
    )
    parser.add_argument("--output-path", default="tinker_cookbook/tinker_backend_cookbook/results/train_kl")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--learning-rate", type=float, default=3e-6)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--clip-epsilon", type=float, default=0.2)
    parser.add_argument(
        "--kl-beta",
        type=float,
        default=0.05,
        help="KL penalty coefficient against the frozen reference. 0 disables the KL term.",
    )
    parser.add_argument("--lora-rank", type=int, default=32)
    parser.add_argument(
        "--eval-after", action="store_true",
        help="Run one fresh same-task episode after training; not a statistical effect estimate.",
    )
    parser.add_argument(
        "--save-state", default=None, metavar="NAME",
        help="Save the training state under NAME after training, before optional evaluation.",
    )
    args = parser.parse_args()

    results_dir = Path(args.output_path) / datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results dir: {results_dir}")
    task = await load_one_task(args)
    write_task_manifest(results_dir, [task], args)
    print(
        "Public task selected locally: "
        f"task_id={task.task_id} dataset={task.dataset} split={task.split} bench={task.bench_name}"
    )

    async with tinker.ServerJob.open(config_path=args.config_path, api_key=args.api_key) as job:
        print(f"ServerJob: {job.job_id} backend={job.base_url}")
        emit_server_job_marker(job)
        client = await job.get_client()
        runtime = await client.create_runtime(
            runtime_type=args.runtime_type,
            config_path=args.config_path,
        )
        async with runtime:
            training_client = await runtime.create_lora_training(
                base_model=args.model_name,
                rank=args.lora_rank,
            )
            sampling_client = await training_client.publish_to_sampler()

            ref_model = args.ref_model_name or args.model_name
            print(f"Creating frozen reference sampling client (base_model={ref_model})")
            ref_sampling_client = await runtime.create_sampling_client(base_model=ref_model)

            train_result = await run_one_task_and_train(
                task=task,
                runtime=runtime,
                training_client=training_client,
                sampling_client=sampling_client,
                ref_sampling_client=ref_sampling_client,
                results_dir=results_dir,
                max_turns=args.max_turns,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                learning_rate=args.learning_rate,
                weight_decay=args.weight_decay,
                clip_epsilon=args.clip_epsilon,
                kl_beta=args.kl_beta,
            )
            if args.save_state is not None:
                checkpoint = await training_client.save_state(args.save_state)
                (results_dir / "checkpoint.json").write_text(json.dumps(
                    {"name": args.save_state, "path": checkpoint.path},
                    ensure_ascii=False, indent=2,
                ))
            if args.eval_after:
                after_sampler = await training_client.publish_to_sampler()
                await evaluate_after_training(
                    task=task, runtime=runtime, sampling_client=after_sampler,
                    results_dir=results_dir, before=train_result,
                    max_turns=args.max_turns, max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )


if __name__ == "__main__":
    asyncio.run(main_async())
