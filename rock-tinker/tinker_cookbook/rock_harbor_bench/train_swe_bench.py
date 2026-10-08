"""Train a model on a single Harbor/SWE task via PPO custom loss.

Mirrors eval_swe_bench.py — runs one task, then does one training step.
Useful for end-to-end debugging before scaling up.

Usage:
    TINKER_API_KEY=tml-xxx python3 tinker_cookbook/rock_harbor_bench/train_swe_bench.py \\
        /path/to/SWE-Env.jsonl \\
        --base-url http://127.0.0.1:9000
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import torch
import tinker

from tinker_cookbook.completers import TinkerTokenCompleter
from tinker_cookbook.renderers import get_renderer
from tinker_cookbook.rl.rollouts import do_single_rollout
from tinker_cookbook.rl.types import Trajectory, Transition
from tinker_cookbook.rock_harbor_bench.env import (
    RemoteSandboxEnv,
    TaskInfo,
    load_harbor_tasks,
)
from tinker_cookbook.utils import model_info, tokenizer_utils
from tinker_cookbook.utils.display import format_trajectory


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
    task_info: TaskInfo,
    training_client: tinker.TrainingClient,
    sampling_client: tinker.SamplingClient,
    tokenizer,
    renderer,
    results_dir: Path,
    max_turns: int = 40,
    max_tokens: int = 8192,
    temperature: float = 0.7,
    learning_rate: float = 3e-6,
    weight_decay: float = 0.0,
    clip_epsilon: float = 0.2,
) -> dict:
    task_name = task_info.get("task_name") or task_info["instance_id"]
    print(f"Task: {task_name}")

    # --- Rollout (identical to eval_swe_bench.py) ---
    env = RemoteSandboxEnv(
        task_info=task_info,
        sampling_client=sampling_client,
        renderer=renderer,
        max_turns=max_turns,
    )
    policy = TinkerTokenCompleter(
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
        format_trajectory(trajectory, tokenizer, only_last_transition=False)
    )

    # --- Training step ---
    examples, old_logprobs, advantages = build_training_examples(trajectory)

    if not examples:
        print("No training examples (all transitions had empty tokens), skipping train step.")
        return {"task_name": task_name, "reward": reward, "turns_used": turns_used,
                "rollout_elapsed": round(rollout_elapsed, 1), "train_skipped": True}

    print(f"Training on {len(examples)} transitions...")
    train_started = time.monotonic()

    loss_fn = make_ppo_loss(old_logprobs, advantages, clip_epsilon)
    fwdbwd_future = await training_client.forward_backward_custom_async(examples, loss_fn)
    fwdbwd_result = await fwdbwd_future.result_async()
    print(f"forward_backward done: {fwdbwd_result.metrics}")

    optim_future = await training_client.optim_step_async(
        tinker.types.AdamParams(learning_rate=learning_rate, weight_decay=weight_decay)
    )
    optim_result = await optim_future.result_async()
    print(f"optim_step done: {optim_result.metrics}")

    new_sampling_client = await training_client.save_weights_and_get_sampling_client_async()
    train_elapsed = time.monotonic() - train_started
    print(f"Weights saved. Train step elapsed={train_elapsed:.1f}s")

    result = {
        "task_name": task_name,
        "reward": reward,
        "turns_used": turns_used,
        "rollout_elapsed": round(rollout_elapsed, 1),
        "train_elapsed": round(train_elapsed, 1),
        "fwdbwd_metrics": fwdbwd_result.metrics,
        "optim_metrics": optim_result.metrics or {},
    }
    (results_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Result: {result}")
    return result


async def main_async() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", help="Path to JSONL Harbor task file")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--model-name", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument("--tokenizer-path")
    parser.add_argument("--renderer-name")
    parser.add_argument("--output-path", default="tinker_cookbook/rock_harbor_bench/train_results")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--learning-rate", type=float, default=3e-6)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--clip-epsilon", type=float, default=0.2)
    parser.add_argument("--lora-rank", type=int, default=32)
    parser.add_argument("--dataset-name", default="rock-harbor-bench")
    parser.add_argument("--dataset-type", default="grpo")
    args = parser.parse_args()

    tasks = load_harbor_tasks(args.data_path)
    if not tasks:
        raise ValueError(f"No tasks found in {args.data_path}")
    task_info = tasks[0]
    task_info.setdefault("dataset_name", args.dataset_name)
    task_info.setdefault("dataset_type", args.dataset_type)

    results_dir = Path(args.output_path) / datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results dir: {results_dir}")

    tokenizer = tokenizer_utils.get_tokenizer(args.tokenizer_path or args.model_name)
    renderer_name = args.renderer_name or model_info.get_recommended_renderer_name(args.model_name)
    renderer = get_renderer(renderer_name, tokenizer)

    tinker_client = tinker.TinkerClient(base_url=args.base_url)
    training_client = tinker_client.create_lora_training_client(
        base_model=args.model_name,
        rank=args.lora_rank,
    )
    sampling_client = training_client.save_weights_and_get_sampling_client()

    await run_one_task_and_train(
        task_info=task_info,
        training_client=training_client,
        sampling_client=sampling_client,
        tokenizer=tokenizer,
        renderer=renderer,
        results_dir=results_dir,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        clip_epsilon=args.clip_epsilon,
    )


if __name__ == "__main__":
    asyncio.run(main_async())
