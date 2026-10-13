"""Train on one public dataset SWE task via the Tinker backend PPO path.

The task is discovered through backend list_tasks and passed as a TaskDescriptor;
no local task file is used by this entrypoint.
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
    """Extract Datum list and per-token advantages from a single trajectory.

    Advantage = total_return (no centering for single trajectory).
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


def _tensor_data_len(value: Any) -> int:
    data = getattr(value, "data", value)
    if isinstance(data, torch.Tensor):
        return int(data.numel())
    if isinstance(data, (list, tuple)):
        return len(data)
    return 0 if data is None else 1


def _datum_total_tokens(datum: tinker.types.Datum) -> int:
    target_tokens = datum.loss_fn_inputs.get("target_tokens")
    return int(datum.model_input.length) + _tensor_data_len(target_tokens)


def select_training_examples(
    examples: list[tinker.types.Datum],
    old_logprobs: list[torch.Tensor],
    advantages: list[torch.Tensor],
    *,
    max_train_transitions: int | None = None,
    max_train_total_tokens: int | None = None,
) -> tuple[list[tinker.types.Datum], list[torch.Tensor], list[torch.Tensor]]:
    """Select a prefix of transitions for bounded-memory live smoke tests."""
    if len(examples) != len(old_logprobs) or len(examples) != len(advantages):
        raise ValueError("examples, old_logprobs, and advantages must have the same length")

    selected_examples: list[tinker.types.Datum] = []
    selected_old_logprobs: list[torch.Tensor] = []
    selected_advantages: list[torch.Tensor] = []
    token_total = 0

    for example, old_lp, adv in zip(examples, old_logprobs, advantages, strict=True):
        if max_train_transitions is not None and max_train_transitions > 0:
            if len(selected_examples) >= max_train_transitions:
                break
        example_tokens = _datum_total_tokens(example)
        if max_train_total_tokens is not None and max_train_total_tokens > 0:
            if selected_examples and token_total + example_tokens > max_train_total_tokens:
                break
        selected_examples.append(example)
        selected_old_logprobs.append(old_lp)
        selected_advantages.append(adv)
        token_total += example_tokens

    return selected_examples, selected_old_logprobs, selected_advantages


def make_ppo_loss(
    old_logprobs: list[torch.Tensor],
    advantages: list[torch.Tensor],
    clip_epsilon: float,
):
    """Clipped PPO surrogate loss: -mean(min(r*A, clip(r,1±ε)*A))."""

    def ppo_loss(
        _data: list[tinker.types.Datum],
        logprobs_list: list[torch.Tensor],
    ) -> tuple[torch.Tensor, dict[str, float]]:
        device = logprobs_list[0].device
        dtype = logprobs_list[0].dtype
        total_loss = torch.zeros((), dtype=dtype, device=device)
        total_ratio = 0.0
        total_clipfrac = 0.0
        total_tokens = 0

        for new_lp, old_lp_cpu, adv_cpu in zip(logprobs_list, old_logprobs, advantages, strict=True):
            old_lp = old_lp_cpu.to(device=device, dtype=dtype)
            adv = adv_cpu.to(device=device, dtype=dtype)
            ratio = (new_lp - old_lp).exp()
            surr1 = ratio * adv
            surr2 = ratio.clamp(1.0 - clip_epsilon, 1.0 + clip_epsilon) * adv
            total_loss = total_loss - torch.minimum(surr1, surr2).sum()
            clipped = ((ratio < 1.0 - clip_epsilon) | (ratio > 1.0 + clip_epsilon)).float()
            total_ratio += float(ratio.detach().sum())
            total_clipfrac += float(clipped.detach().sum())
            total_tokens += ratio.numel()

        if total_tokens == 0:
            raise ValueError("ppo_loss: zero tokens")

        total_loss = total_loss / total_tokens
        metrics = {
            "trainer/ppo_loss": float(total_loss.detach()),
            "trainer/ratio_mean": total_ratio / total_tokens,
            "trainer/clipfrac": total_clipfrac / total_tokens,
        }
        return total_loss, metrics

    return ppo_loss


async def run_one_task_and_train(
    task: tinker.TaskDescriptor,
    runtime: Any,
    training_client: tinker.TrainingClient,
    sampling_client: tinker.SamplingClient,
    results_dir: Path,
    max_turns: int = 40,
    max_tokens: int = 8192,
    temperature: float = 0.7,
    learning_rate: float = 3e-6,
    weight_decay: float = 0.0,
    clip_epsilon: float = 0.2,
    max_train_transitions: int | None = None,
    max_train_total_tokens: int | None = None,
) -> dict:
    task_label = task_name(task)
    print(f"Task: {task_label}")

    # --- Rollout (identical to eval_swe_bench.py) ---
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
    total_examples = len(examples)
    total_train_tokens = sum(_datum_total_tokens(example) for example in examples)
    examples, old_logprobs, advantages = select_training_examples(
        examples,
        old_logprobs,
        advantages,
        max_train_transitions=max_train_transitions,
        max_train_total_tokens=max_train_total_tokens,
    )

    if not examples:
        print("No training examples (all transitions had empty tokens), skipping train step.")
        return {"task_name": task_label, "reward": reward, "turns_used": turns_used,
                "rollout_elapsed": round(rollout_elapsed, 1), "train_skipped": True}

    selected_train_tokens = sum(_datum_total_tokens(example) for example in examples)
    print(
        "Training on "
        f"{len(examples)}/{total_examples} transitions, "
        f"tokens={selected_train_tokens}/{total_train_tokens}."
    )
    train_started = time.monotonic()

    loss_fn = make_ppo_loss(old_logprobs, advantages, clip_epsilon)
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
        "train_elapsed": round(train_elapsed, 1),
        "train_transitions": len(examples),
        "train_tokens": selected_train_tokens,
        "train_total_transitions": total_examples,
        "train_total_tokens": total_train_tokens,
        "fwdbwd_metrics": fwdbwd_result.metrics,
        "optim_metrics": optim_result.metrics or {},
        "published_sampling_client": new_sampling_client is not None,
    }
    (results_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Result: {result}")
    return result


async def evaluate_after_training(
    *,
    task: tinker.TaskDescriptor,
    runtime: Any,
    sampling_client: tinker.SamplingClient,
    results_dir: Path,
    before: dict[str, Any],
    max_turns: int,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """One fresh same-task episode; this comparison is not a statistical estimate."""
    after_dir = results_dir / "after"
    after_dir.mkdir(parents=True, exist_ok=False)
    env = RemoteSandboxEnv(task=task, runtime=runtime, max_turns=max_turns)
    policy = RuntimePromptCompleter(
        sampling_client=sampling_client, max_tokens=max_tokens, temperature=temperature,
    )
    started = time.monotonic()
    # Let real rollout errors propagate; an infrastructure failure is not reward 0.
    trajectory = await do_single_rollout(policy, env)
    after = {
        "task_name": task_name(task),
        "reward": sum(transition.reward for transition in trajectory.transitions),
        "turns_used": len(trajectory.transitions),
        "episode_done": bool(trajectory.transitions and trajectory.transitions[-1].episode_done),
        "env_id": env.get_env_id(),
        "rollout_elapsed": round(time.monotonic() - started, 1),
    }
    result = {
        "comparison": "same_task_single_after_episode_not_statistical",
        "before": before,
        "after": after,
        "budget": {"max_turns": max_turns, "max_tokens": max_tokens, "temperature": temperature},
    }
    (after_dir / "trajectory.txt").write_text(
        format_structured_trajectory(trajectory, only_last_transition=False),
    )
    (after_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Single after-training episode (not a statistical effect): {after}")
    return result


async def main_async() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    add_task_args(parser)
    parser.add_argument("--api-key", default="tml-dummy")
    parser.add_argument("--runtime-type", default="ROLL")
    parser.add_argument("--config-path", default=str(Path(__file__).resolve().parents[1] / "config" / "roll_train_runtime.yaml"))
    parser.add_argument("--model-name", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument("--output-path", default="tinker_cookbook/tinker_backend_cookbook/results/train")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--learning-rate", type=float, default=3e-6)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--clip-epsilon", type=float, default=0.2)
    parser.add_argument("--max-train-transitions", type=int)
    parser.add_argument("--max-train-total-tokens", type=int)
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

            train_result = await run_one_task_and_train(
                task=task,
                runtime=runtime,
                training_client=training_client,
                sampling_client=sampling_client,
                results_dir=results_dir,
                max_turns=args.max_turns,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                learning_rate=args.learning_rate,
                weight_decay=args.weight_decay,
                clip_epsilon=args.clip_epsilon,
                max_train_transitions=args.max_train_transitions,
                max_train_total_tokens=args.max_train_total_tokens,
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
