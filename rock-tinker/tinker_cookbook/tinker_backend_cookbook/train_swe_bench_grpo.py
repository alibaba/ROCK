"""Train via PPO + GRPO group-relative baseline + frozen reference KL anchor.

Each rollout uses a TaskDescriptor selected locally before backend startup.
The runtime still owns env creation, sampling, reference scoring, and training.
"""

from __future__ import annotations

import argparse
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

# --------------------------------------------------------------------------- #
# Per-trajectory training material
# --------------------------------------------------------------------------- #

def build_training_material_for_trajectory(
    trajectory: Trajectory,
    advantage_value: float,
) -> tuple[list[tinker.types.Datum], list[torch.Tensor], list[torch.Tensor]]:
    """Build PPO examples for one trajectory with a single broadcast advantage.

    Mirrors train_swe_bench_kl.build_training_examples but the per-token
    advantage is supplied externally (computed from group statistics) rather
    than taking the trajectory's raw total_return.
    """
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
            torch.full(
                (len(transition.ac.tokens),), advantage_value, dtype=torch.float32
            )
        )

    return examples, old_logprobs_list, advantages_list


# --------------------------------------------------------------------------- #
# GRPO baseline
# --------------------------------------------------------------------------- #

def compute_grpo_advantages(returns: list[float], eps: float = 1e-6) -> list[float]:
    """Normalize a list of trajectory-level returns within the group.

        A_i = (r_i - mean) / (std + eps)

    If std == 0 (e.g. all rollouts got reward=0), every advantage collapses
    to 0; the caller should warn that this step's PPO contribution will be
    ~0 (only the KL term gradients flow).
    """
    if not returns:
        return []
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    std_r = var_r ** 0.5
    return [(r - mean_r) / (std_r + eps) for r in returns]


# --------------------------------------------------------------------------- #
# Reference logprobs (copied verbatim from train_swe_bench_kl.py)
# --------------------------------------------------------------------------- #

async def compute_ref_logprobs_for_responses(
    ref_client: tinker.SamplingClient,
    examples: list[tinker.types.Datum],
) -> list[torch.Tensor]:
    """Score each example's response tokens under the frozen reference."""
    tasks = []
    prompt_lens: list[int] = []
    for ex in examples:
        prompt_tokens = ex.model_input.to_ints()
        response_tokens = ex.loss_fn_inputs["target_tokens"].tolist()
        full_input = tinker.types.ModelInput.from_ints(
            prompt_tokens + response_tokens
        )
        prompt_lens.append(len(prompt_tokens))
        tasks.append(asyncio.wrap_future(ref_client.compute_logprobs(full_input)))

    all_results = await asyncio.gather(*tasks)

    out: list[torch.Tensor] = []
    for full_lp, prompt_len in zip(all_results, prompt_lens, strict=True):
        resp_lp = full_lp[prompt_len:]
        resp_lp_floats = [float(lp) if lp is not None else 0.0 for lp in resp_lp]
        out.append(torch.tensor(resp_lp_floats, dtype=torch.float32))
    return out


# --------------------------------------------------------------------------- #
# PPO + KL loss (identical to train_swe_bench_kl.make_ppo_loss_with_kl)
# --------------------------------------------------------------------------- #

def make_ppo_loss_with_kl(
    old_logprobs: list[torch.Tensor],
    ref_logprobs: list[torch.Tensor],
    advantages: list[torch.Tensor],
    clip_epsilon: float,
    kl_beta: float,
):
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

            # PPO clipped surrogate
            ratio = (new_lp - old_lp).exp()
            surr1 = ratio * adv
            surr2 = ratio.clamp(1.0 - clip_epsilon, 1.0 + clip_epsilon) * adv
            total_loss = total_loss - torch.minimum(surr1, surr2).sum()

            # Schulman k3 KL penalty
            log_ratio_ref = new_lp - ref_lp
            ratio_ref = log_ratio_ref.exp()
            kl_per_token = ratio_ref - 1.0 - log_ratio_ref
            total_loss = total_loss + kl_beta * kl_per_token.sum()

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
        return total_loss, {
            "trainer/ppo_kl_loss": float(total_loss.detach()),
            "trainer/ratio_mean": total_ratio / total_tokens,
            "trainer/clipfrac": total_clipfrac / total_tokens,
            "trainer/kl_per_token": total_kl / total_tokens,
            "trainer/kl_ref_log_ratio_mean": total_kl_ref_log_ratio / total_tokens,
            "trainer/kl_beta": kl_beta,
        }

    return ppo_loss


# --------------------------------------------------------------------------- #
# Parallel rollout
# --------------------------------------------------------------------------- #

async def rollout_one(
    task: tinker.TaskDescriptor,
    runtime: Any,
    sampling_client: tinker.SamplingClient,
    rollout_idx: int,
    max_turns: int,
    max_tokens: int,
    temperature: float,
) -> tuple[int, Trajectory | None, Exception | None]:
    """Run a single rollout. Each call instantiates its own RemoteSandboxEnv
    so the backend assigns it a unique env_id and Harbor sandbox.

    Returns (idx, trajectory_or_None, error_or_None).
    """
    try:
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
        traj = await do_single_rollout(policy, env)
        return rollout_idx, traj, None
    except Exception as e:  # noqa: BLE001 — we deliberately tolerate per-rollout failure
        return rollout_idx, None, e


async def gather_group_rollouts(
    task: tinker.TaskDescriptor,
    runtime: Any,
    sampling_client: tinker.SamplingClient,
    group_size: int,
    concurrency: int,
    max_turns: int,
    max_tokens: int,
    temperature: float,
) -> list[tuple[int, Trajectory | None, Exception | None]]:
    """Spawn `group_size` rollouts of the same task in parallel.

    Concurrency is bounded by an asyncio.Semaphore — set concurrency
    == group_size to fully parallelize. The shared sampling_client is
    safe across concurrent rollouts; demuxing happens on the backend
    by env_id (each rollout has a different env_id).
    """
    sem = asyncio.Semaphore(concurrency)

    async def bounded(idx: int):
        async with sem:
            print(f"  [rollout {idx + 1}/{group_size}] starting")
            t0 = time.monotonic()
            res = await rollout_one(
                task=task,
                runtime=runtime,
                sampling_client=sampling_client,
                rollout_idx=idx,
                max_turns=max_turns,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            elapsed = time.monotonic() - t0
            _, traj, err = res
            if err is not None:
                print(
                    f"  [rollout {idx + 1}/{group_size}] FAILED in {elapsed:.1f}s: {err!r}"
                )
            else:
                r = sum(t.reward for t in traj.transitions)
                turns = len(traj.transitions)
                print(
                    f"  [rollout {idx + 1}/{group_size}] done in {elapsed:.1f}s, "
                    f"turns={turns}, reward={r:.3f}"
                )
            return res

    return await asyncio.gather(*(bounded(i) for i in range(group_size)))


# --------------------------------------------------------------------------- #
# Main GRPO step
# --------------------------------------------------------------------------- #

async def run_one_grpo_step(
    task: tinker.TaskDescriptor,
    runtime: Any,
    training_client: tinker.TrainingClient,
    sampling_client: tinker.SamplingClient,
    ref_sampling_client: tinker.SamplingClient,
    results_dir: Path,
    group_size: int,
    rollout_concurrency: int,
    max_turns: int,
    max_tokens: int,
    temperature: float,
    learning_rate: float,
    weight_decay: float,
    clip_epsilon: float,
    kl_beta: float,
) -> tuple[dict[str, Any], tinker.SamplingClient]:
    task_label = task_name(task)
    print(
        f"\n=== GRPO step on task: {task_label} "
        f"(group_size={group_size}, concurrency={rollout_concurrency}) ==="
    )

    # ----------------------------- 1. Parallel rollouts ----------------- #
    rollout_started = time.monotonic()
    results = await gather_group_rollouts(
        task=task,
        runtime=runtime,
        sampling_client=sampling_client,
        group_size=group_size,
        concurrency=rollout_concurrency,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    rollout_elapsed = time.monotonic() - rollout_started

    successful: list[tuple[int, Trajectory]] = []
    failed: list[tuple[int, Exception]] = []
    for idx, traj, err in results:
        if err is not None:
            failed.append((idx, err))
        else:
            successful.append((idx, traj))

    print(
        f"Group rollouts done: {len(successful)}/{group_size} succeeded "
        f"in {rollout_elapsed:.1f}s (wall, parallel)"
    )
    if len(successful) < 2:
        print("Need ≥2 successful trajectories for GRPO baseline; skipping step.")
        return {
            "task_name": task_label,
            "rollouts_total": group_size,
            "rollouts_succeeded": len(successful),
            "skipped": "need_ge_2_successful_rollouts",
            "rollout_elapsed": round(rollout_elapsed, 1),
        }, sampling_client

    # ----------------------------- 2. GRPO advantages ------------------- #
    returns = [sum(t.reward for t in traj.transitions) for _, traj in successful]
    advantages = compute_grpo_advantages(returns)
    group_mean = sum(returns) / len(returns)
    group_std = (sum((r - group_mean) ** 2 for r in returns) / len(returns)) ** 0.5

    print(
        f"Group stats: returns={[round(r, 3) for r in returns]}, "
        f"mean={group_mean:.3f}, std={group_std:.4f}"
    )
    print(f"GRPO advantages: {[round(a, 3) for a in advantages]}")

    if group_std < 1e-8:
        print(
            "WARNING: group_std≈0 (all trajectories got identical reward). "
            "All advantages collapse to 0 → only KL term contributes."
        )

    # Save per-rollout trajectories
    for (idx, traj), ret, adv in zip(successful, returns, advantages):
        (results_dir / f"rollout_{idx:02d}.txt").write_text(
            format_structured_trajectory(traj, only_last_transition=False)
        )
        (results_dir / f"rollout_{idx:02d}.json").write_text(
            json.dumps(
                {
                    "rollout_idx": idx,
                    "return": ret,
                    "advantage": adv,
                    "turns": len(traj.transitions),
                },
                indent=2,
            )
        )

    # ----------------------------- 3. Assemble training material -------- #
    all_examples: list[tinker.types.Datum] = []
    all_old_logprobs: list[torch.Tensor] = []
    all_advantages: list[torch.Tensor] = []
    for (idx, traj), adv_value in zip(successful, advantages):
        ex, lp, av = build_training_material_for_trajectory(traj, adv_value)
        all_examples.extend(ex)
        all_old_logprobs.extend(lp)
        all_advantages.extend(av)

    if not all_examples:
        print("All trajectories produced empty action tokens; nothing to train on.")
        return {
            "task_name": task_label,
            "rollouts_total": group_size,
            "rollouts_succeeded": len(successful),
            "skipped": "no_action_tokens",
            "rollout_elapsed": round(rollout_elapsed, 1),
        }, sampling_client

    # ----------------------------- 4. Ref logprobs ---------------------- #
    print(
        f"Computing ref logprobs for {len(all_examples)} transition(s) "
        f"across {len(successful)} trajectory(ies)..."
    )
    ref_started = time.monotonic()
    all_ref_logprobs = await compute_ref_logprobs_for_responses(
        ref_sampling_client, all_examples
    )
    ref_elapsed = time.monotonic() - ref_started
    print(f"  ref logprobs done in {ref_elapsed:.1f}s")

    # ----------------------------- 5. forward_backward + optim_step ----- #
    print(f"Training: {len(all_examples)} transitions, kl_beta={kl_beta}")
    train_started = time.monotonic()

    loss_fn = make_ppo_loss_with_kl(
        all_old_logprobs, all_ref_logprobs, all_advantages, clip_epsilon, kl_beta
    )
    fwdbwd_future = training_client.forward_backward_custom(
        all_examples, loss_fn
    )
    fwdbwd_result = await fwdbwd_future
    print(f"forward_backward done: {fwdbwd_result.metrics}")

    optim_future = training_client.optim_step(
        tinker.types.AdamParams(
            learning_rate=learning_rate, weight_decay=weight_decay
        )
    )
    optim_result = await optim_future
    print(f"optim_step done: {optim_result.metrics}")

    sampling_client = await training_client.publish_to_sampler()
    train_elapsed = time.monotonic() - train_started
    print(f"Weights published to sampler. Train step elapsed={train_elapsed:.1f}s")

    result = {
        "task_name": task_label,
        "rollouts_total": group_size,
        "rollouts_succeeded": len(successful),
        "rollouts_failed": len(failed),
        "group_returns": returns,
        "group_mean": group_mean,
        "group_std": group_std,
        "group_advantages": advantages,
        "total_transitions": len(all_examples),
        "rollout_elapsed": round(rollout_elapsed, 1),
        "ref_logprobs_elapsed": round(ref_elapsed, 1),
        "train_elapsed": round(train_elapsed, 1),
        "fwdbwd_metrics": fwdbwd_result.metrics,
        "optim_metrics": optim_result.metrics or {},
    }
    (results_dir / "step_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(f"GRPO step result: {result}")
    return result, sampling_client


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def main_async() -> None:
    parser = argparse.ArgumentParser()
    add_task_args(parser)
    parser.add_argument("--api-key", default="tml-dummy")
    parser.add_argument("--runtime-type", default="ROLL")
    parser.add_argument("--config-path", default=str(Path(__file__).resolve().parents[1] / "config" / "tinker_backend_cookbook" / "roll_train_runtime.yaml"))
    parser.add_argument("--model-name", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument(
        "--ref-model-name",
        default=None,
        help="Base model for the frozen KL reference. Defaults to --model-name.",
    )
    parser.add_argument(
        "--output-path",
        default="tinker_cookbook/tinker_backend_cookbook/results/train_grpo",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--num-steps", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=3e-6)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--clip-epsilon", type=float, default=0.2)
    parser.add_argument(
        "--kl-beta",
        type=float,
        default=0.05,
        help="KL penalty coefficient against frozen reference.",
    )
    parser.add_argument("--lora-rank", type=int, default=32)
    parser.add_argument(
        "--group-size",
        type=int,
        default=8,
        help="N trajectories per prompt for GRPO baseline (default 8).",
    )
    parser.add_argument(
        "--rollout-concurrency",
        type=int,
        default=None,
        help="Max parallel rollouts. Defaults to --group-size (full parallel).",
    )
    parser.add_argument(
        "--eval-after", action="store_true",
        help="Run one fresh same-task episode after training; not a statistical effect estimate.",
    )
    parser.add_argument(
        "--save-state", default=None, metavar="NAME",
        help="Save the training state under NAME after training, before optional evaluation.",
    )
    args = parser.parse_args()

    if args.group_size < 2:
        parser.error("--group-size must be ≥ 2 (GRPO needs intra-group variance)")
    if args.num_steps < 1:
        parser.error("--num-steps must be ≥ 1")
    concurrency = args.rollout_concurrency or args.group_size
    if concurrency < 1:
        parser.error("--rollout-concurrency must be ≥ 1")

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

            step_results: list[dict[str, Any]] = []
            for step_idx in range(args.num_steps):
                step_dir = results_dir / f"step_{step_idx:03d}"
                step_dir.mkdir(parents=True, exist_ok=True)
                print(f"\n##### GRPO loop {step_idx + 1}/{args.num_steps} #####")
                step_result, sampling_client = await run_one_grpo_step(
                    task=task,
                    runtime=runtime,
                    training_client=training_client,
                    sampling_client=sampling_client,
                    ref_sampling_client=ref_sampling_client,
                    results_dir=step_dir,
                    group_size=args.group_size,
                    rollout_concurrency=concurrency,
                    max_turns=args.max_turns,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    learning_rate=args.learning_rate,
                    weight_decay=args.weight_decay,
                    clip_epsilon=args.clip_epsilon,
                    kl_beta=args.kl_beta,
                )
                step_result["step_idx"] = step_idx
                step_results.append(step_result)

            summary = {
                "num_steps": args.num_steps,
                "group_size": args.group_size,
                "rollout_concurrency": concurrency,
                "temperature": args.temperature,
                "kl_beta": args.kl_beta,
                "steps": step_results,
            }
            (results_dir / "result.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2)
            )
            print(f"GRPO run summary: {summary}")
            if args.save_state is not None:
                checkpoint = await training_client.save_state(args.save_state)
                (results_dir / "checkpoint.json").write_text(json.dumps(
                    {"name": args.save_state, "path": checkpoint.path},
                    ensure_ascii=False, indent=2,
                ))
            if args.eval_after:
                await evaluate_after_training(
                    task=task, runtime=runtime, sampling_client=sampling_client,
                    results_dir=results_dir, before=step_results[-1],
                    max_turns=args.max_turns, max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )


if __name__ == "__main__":
    asyncio.run(main_async())
