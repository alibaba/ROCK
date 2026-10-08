"""Train via PPO + GRPO group-relative baseline + frozen reference KL anchor.

Same loss skeleton as train_swe_bench_kl.py:
    loss = -mean(min(r·A, clip(r, 1±ε)·A)) + β · mean(kl_k3)

But A is computed from GRPO group statistics instead of raw total_return:
    A_traj = (total_return_traj - group_mean) / (group_std + eps)

For each prompt, spawn N parallel rollouts. Each rollout gets its own
RemoteSandboxEnv (=> backend init_task_env produces an independent env_id
with an independent Harbor sandbox). Once all N complete, normalize
advantages within the group, then run a single forward_backward over all
N trajectories' transitions concatenated.

Requires ROLL backend with enable_reference: true (same as
train_swe_bench_kl.py). No backend code changes are needed for GRPO —
ROLL already demuxes concurrent env_ids on _PipelineStateActor.

Usage:
    TINKER_API_KEY=tml-test python3 train_swe_bench_grpo.py \\
        SWE-Bench-Verified-sympy-19637.jsonl \\
        --group-size 8 \\
        --kl-beta 0.05 \\
        --learning-rate 3e-6
"""

from __future__ import annotations

import argparse
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
from tinker_cookbook.rl.types import Trajectory
from tinker_cookbook.rock_harbor_bench.env import (
    RemoteSandboxEnv,
    TaskInfo,
    load_harbor_tasks,
)
from tinker_cookbook.utils import model_info, tokenizer_utils
from tinker_cookbook.utils.display import format_trajectory


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
        tasks.append(ref_client.compute_logprobs_async(full_input))

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
    task_info: TaskInfo,
    sampling_client: tinker.SamplingClient,
    renderer,
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
        traj = await do_single_rollout(policy, env)
        return rollout_idx, traj, None
    except Exception as e:  # noqa: BLE001 — we deliberately tolerate per-rollout failure
        return rollout_idx, None, e


async def gather_group_rollouts(
    task_info: TaskInfo,
    sampling_client: tinker.SamplingClient,
    renderer,
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
                task_info=task_info,
                sampling_client=sampling_client,
                renderer=renderer,
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
    task_info: TaskInfo,
    training_client: tinker.TrainingClient,
    sampling_client: tinker.SamplingClient,
    ref_sampling_client: tinker.SamplingClient,
    tokenizer,
    renderer,
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
) -> dict:
    task_name = task_info.get("task_name") or task_info["instance_id"]
    print(
        f"\n=== GRPO step on task: {task_name} "
        f"(group_size={group_size}, concurrency={rollout_concurrency}) ==="
    )

    # ----------------------------- 1. Parallel rollouts ----------------- #
    rollout_started = time.monotonic()
    results = await gather_group_rollouts(
        task_info=task_info,
        sampling_client=sampling_client,
        renderer=renderer,
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
            "task_name": task_name,
            "rollouts_total": group_size,
            "rollouts_succeeded": len(successful),
            "skipped": "need_ge_2_successful_rollouts",
            "rollout_elapsed": round(rollout_elapsed, 1),
        }

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
            format_trajectory(traj, tokenizer, only_last_transition=False)
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
            "task_name": task_name,
            "rollouts_total": group_size,
            "rollouts_succeeded": len(successful),
            "skipped": "no_action_tokens",
            "rollout_elapsed": round(rollout_elapsed, 1),
        }

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
    fwdbwd_future = await training_client.forward_backward_custom_async(
        all_examples, loss_fn
    )
    fwdbwd_result = await fwdbwd_future.result_async()
    print(f"forward_backward done: {fwdbwd_result.metrics}")

    optim_future = await training_client.optim_step_async(
        tinker.types.AdamParams(
            learning_rate=learning_rate, weight_decay=weight_decay
        )
    )
    optim_result = await optim_future.result_async()
    print(f"optim_step done: {optim_result.metrics}")

    _ = await training_client.save_weights_and_get_sampling_client_async()
    train_elapsed = time.monotonic() - train_started
    print(f"Weights saved. Train step elapsed={train_elapsed:.1f}s")

    result = {
        "task_name": task_name,
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
    return result


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def main_async() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", help="Path to JSONL Harbor task file")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--model-name", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument(
        "--ref-model-name",
        default=None,
        help="Base model for the frozen KL reference. Defaults to --model-name.",
    )
    parser.add_argument("--tokenizer-path")
    parser.add_argument("--renderer-name")
    parser.add_argument(
        "--output-path",
        default="tinker_cookbook/rock_harbor_bench/train_results_grpo",
    )
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
        help="KL penalty coefficient against frozen reference.",
    )
    parser.add_argument("--lora-rank", type=int, default=32)
    parser.add_argument("--dataset-name", default="rock-harbor-bench")
    parser.add_argument("--dataset-type", default="grpo")
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
    args = parser.parse_args()

    if args.group_size < 2:
        parser.error("--group-size must be ≥ 2 (GRPO needs intra-group variance)")
    concurrency = args.rollout_concurrency or args.group_size
    if concurrency < 1:
        parser.error("--rollout-concurrency must be ≥ 1")

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
    renderer_name = args.renderer_name or model_info.get_recommended_renderer_name(
        args.model_name
    )
    renderer = get_renderer(renderer_name, tokenizer)

    tinker_client = tinker.TinkerClient(base_url=args.base_url)

    training_client = tinker_client.create_lora_training_client(
        base_model=args.model_name,
        rank=args.lora_rank,
    )
    sampling_client = training_client.save_weights_and_get_sampling_client()

    ref_model = args.ref_model_name or args.model_name
    print(f"Creating frozen reference sampling client (base_model={ref_model})")
    ref_sampling_client = tinker_client.create_sampling_client(base_model=ref_model)

    await run_one_grpo_step(
        task_info=task_info,
        training_client=training_client,
        sampling_client=sampling_client,
        ref_sampling_client=ref_sampling_client,
        tokenizer=tokenizer,
        renderer=renderer,
        results_dir=results_dir,
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


if __name__ == "__main__":
    asyncio.run(main_async())
