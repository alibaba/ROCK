"""
Standalone training for Harbor tasks via PPO custom loss.

Mirrors eval.py structure. Each training step:
  1. Roll out a batch of tasks concurrently (same as run_eval)
  2. Build training examples with centered advantages
  3. forward_backward_custom + optim_step
  4. save_weights → new sampling_client for next step
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import chz
import tinker
import torch

from tinker_cookbook.completers import TinkerTokenCompleter
from tinker_cookbook.renderers import get_renderer
from tinker_cookbook.renderers.base import Renderer
from tinker_cookbook.rl.rollouts import do_single_rollout
from tinker_cookbook.rl.types import Trajectory
from tinker_cookbook.rock_harbor_bench.env import RemoteSandboxEnv, TaskInfo
from tinker_cookbook.utils import model_info, tokenizer_utils
from tinker_cookbook.utils.ml_log import dump_config

logger = logging.getLogger(__name__)


@dataclass
class HarborTask:
    """A Harbor task definition."""

    task_name: str
    """Unique task identifier"""


@chz.chz
class TrainConfig:
    """Configuration for Harbor PPO training."""

    model_name: str = "moonshotai/Kimi-K2-Thinking"
    output_path: str = "tinker_cookbook/recipes/harbor_rl/scripts/train_results"
    max_turns: int = 10
    max_tokens: int = 2048
    temperature: float = 0.7
    base_url: str | None = None
    renderer_name: str | None = None
    dataset_name: str = "swebench-verified"
    dataset_type: str = "grpo"
    tokenizer_path: str | None = None
    train_steps: int = 10
    batch_size: int = 4
    learning_rate: float = 3e-6
    weight_decay: float = 0.0
    clip_epsilon: float = 0.2
    lora_rank: int = 32
    save_every: int = 0
    resume_path: str | None = None


@dataclass
class StepResult:
    step: int
    trajectories: int
    return_mean: float
    return_max: float
    return_min: float
    ppo_loss: float
    elapsed_seconds: float
    error: str | None = None


# ---------------------------------------------------------------------------
# Training data helpers
# ---------------------------------------------------------------------------

def build_training_examples(
    trajectories: list[Trajectory],
) -> tuple[list[tinker.types.Datum], list[torch.Tensor], list[torch.Tensor], dict[str, float]]:
    """Extract Datum list and per-token advantages from a batch of trajectories.

    Advantages are centered across the batch:
        advantage_i = total_return_i - mean(total_returns)
    """
    returns = [sum(t.reward for t in traj.transitions) for traj in trajectories]
    mean_return = sum(returns) / len(returns) if returns else 0.0

    examples: list[tinker.types.Datum] = []
    old_logprobs_list: list[torch.Tensor] = []
    advantages_list: list[torch.Tensor] = []
    transition_count = 0
    token_count = 0

    for traj, total_return in zip(trajectories, returns):
        centered_adv = float(total_return - mean_return)
        for transition in traj.transitions:
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
                torch.full((len(transition.ac.tokens),), centered_adv, dtype=torch.float32)
            )
            transition_count += 1
            token_count += len(transition.ac.tokens)

    stats = {
        "return_mean": float(mean_return),
        "return_max": float(max(returns)) if returns else 0.0,
        "return_min": float(min(returns)) if returns else 0.0,
        "transitions": float(transition_count),
        "tokens": float(token_count),
    }
    return examples, old_logprobs_list, advantages_list, stats


def make_ppo_loss(
    old_logprobs: list[torch.Tensor],
    advantages: list[torch.Tensor],
    clip_epsilon: float,
):
    """Clipped PPO surrogate loss: -mean(min(r*A, clip(r, 1±ε)*A))."""

    def ppo_loss(
        _data: list[tinker.types.Datum],
        logprobs_list: list[torch.Tensor],
    ) -> tuple[torch.Tensor, dict[str, float]]:
        if not logprobs_list:
            raise ValueError("ppo_loss received no logprobs")

        device = logprobs_list[0].device
        dtype = logprobs_list[0].dtype
        total_loss = torch.zeros((), dtype=dtype, device=device)
        total_ratio = 0.0
        total_clipfrac = 0.0
        total_tokens = 0

        for new_lp, old_lp_cpu, adv_cpu in zip(logprobs_list, old_logprobs, advantages):
            old_lp = old_lp_cpu.to(device=device, dtype=dtype)
            adv = adv_cpu.to(device=device, dtype=dtype)
            ratio = (new_lp - old_lp).exp()
            surr1 = ratio * adv
            surr2 = ratio.clamp(1.0 - clip_epsilon, 1.0 + clip_epsilon) * adv
            total_loss = total_loss - torch.minimum(surr1, surr2).sum()
            clipped = ((ratio < 1.0 - clip_epsilon) | (ratio > 1.0 + clip_epsilon)).float()
            total_ratio += float(ratio.detach().sum().item())
            total_clipfrac += float(clipped.detach().sum().item())
            total_tokens += ratio.numel()

        if total_tokens == 0:
            raise ValueError("ppo_loss received zero tokens")

        total_loss = total_loss / total_tokens
        metrics = {
            "trainer/ppo_loss": float(total_loss.detach().item()),
            "trainer/ratio_mean": total_ratio / total_tokens,
            "trainer/clipfrac": total_clipfrac / total_tokens,
        }
        return total_loss, metrics

    return ppo_loss


# ---------------------------------------------------------------------------
# Rollout (mirrors evaluate_task in eval.py)
# ---------------------------------------------------------------------------

async def rollout_task(
    task: HarborTask,
    policy: TinkerTokenCompleter,
    renderer: Renderer,
    sampling_client: tinker.SamplingClient,
    config: TrainConfig,
) -> Trajectory | None:
    """Roll out a single task. Returns None on failure."""
    try:
        task_info: TaskInfo = {
            "task_name": task.task_name,
            "instance_id": task.task_name,
            "dataset_name": config.dataset_name,
            "dataset_type": config.dataset_type,
        }
        env = RemoteSandboxEnv(
            task_info=task_info,
            sampling_client=sampling_client,
            renderer=renderer,
            max_turns=config.max_turns,
        )
        return await do_single_rollout(policy, env)
    except Exception as e:
        logger.warning("Rollout failed for task %s: %s", task.task_name, e)
        return None


# ---------------------------------------------------------------------------
# Main training loop (mirrors run_eval in eval.py)
# ---------------------------------------------------------------------------

async def run_train(
    config: TrainConfig,
    tasks: list[HarborTask],
) -> list[StepResult]:
    """Run PPO training over a list of Harbor tasks.

    Each step samples a batch of tasks, runs rollouts concurrently,
    then does one forward_backward + optim_step update.

    Args:
        config: Training configuration.
        tasks: Full task pool to sample batches from.

    Returns:
        List of per-step results.
    """
    results_dir = Path(config.output_path) / datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Results dir: %s", results_dir)

    config_dict = dump_config(config)
    (results_dir / "config.json").write_text(json.dumps(config_dict, indent=2))

    tinker_client = tinker.TinkerClient(base_url=config.base_url)
    if config.resume_path:
        training_client = tinker_client.create_training_client_from_state_with_optimizer(
            config.resume_path
        )
    else:
        training_client = tinker_client.create_lora_training_client(
            base_model=config.model_name,
            rank=config.lora_rank,
        )

    sampling_client = training_client.save_weights_and_get_sampling_client()

    tokenizer = tokenizer_utils.get_tokenizer(config.tokenizer_path or config.model_name)
    renderer_name = config.renderer_name or model_info.get_recommended_renderer_name(
        config.model_name
    )
    renderer = get_renderer(renderer_name, tokenizer)

    metrics_path = results_dir / "metrics.jsonl"
    step_results: list[StepResult] = []

    for step in range(config.train_steps):
        started = time.monotonic()

        start_idx = step * config.batch_size
        task_batch = [tasks[(start_idx + i) % len(tasks)] for i in range(config.batch_size)]

        logger.info(
            "Step %d/%d — rolling out %d tasks: %s",
            step + 1, config.train_steps, len(task_batch),
            [t.task_name for t in task_batch],
        )

        policy = TinkerTokenCompleter(
            sampling_client=sampling_client,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

        # Concurrent rollout (same as run_eval's asyncio.gather)
        raw = await asyncio.gather(
            *[rollout_task(task, policy, renderer, sampling_client, config) for task in task_batch]
        )
        trajectories = [t for t in raw if t is not None]

        if not trajectories:
            logger.warning("Step %d: all rollouts failed, skipping.", step)
            step_results.append(StepResult(
                step=step, trajectories=0,
                return_mean=0.0, return_max=0.0, return_min=0.0,
                ppo_loss=float("nan"),
                elapsed_seconds=round(time.monotonic() - started, 1),
                error="all rollouts failed",
            ))
            continue

        failed = len(task_batch) - len(trajectories)
        if failed:
            logger.warning("Step %d: %d/%d rollouts failed.", step, failed, len(task_batch))

        examples, old_logprobs, advantages, rollout_stats = build_training_examples(trajectories)
        if not examples:
            logger.warning("Step %d: no training examples, skipping.", step)
            continue

        loss_fn = make_ppo_loss(old_logprobs, advantages, config.clip_epsilon)
        fwdbwd_future = await training_client.forward_backward_custom_async(examples, loss_fn)
        fwdbwd_result = await fwdbwd_future.result_async()

        optim_future = await training_client.optim_step_async(
            tinker.types.AdamParams(
                learning_rate=config.learning_rate,
                weight_decay=config.weight_decay,
            )
        )
        optim_result = await optim_future.result_async()

        sampling_client = await training_client.save_weights_and_get_sampling_client_async()

        ppo_loss = fwdbwd_result.metrics.get("trainer/ppo_loss", float("nan"))
        elapsed = round(time.monotonic() - started, 1)

        step_result = StepResult(
            step=step,
            trajectories=len(trajectories),
            return_mean=rollout_stats["return_mean"],
            return_max=rollout_stats["return_max"],
            return_min=rollout_stats["return_min"],
            ppo_loss=ppo_loss,
            elapsed_seconds=elapsed,
        )
        step_results.append(step_result)

        metrics = {
            "step": step,
            "rollout": rollout_stats,
            "fwdbwd": fwdbwd_result.metrics,
            "optim": optim_result.metrics or {},
            "elapsed_seconds": elapsed,
        }
        with metrics_path.open("a") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")

        logger.info(
            "Step %d done — return_mean=%.3f  ppo_loss=%.4f  elapsed=%.1fs",
            step, rollout_stats["return_mean"], ppo_loss, elapsed,
        )

        if config.save_every > 0 and (step + 1) % config.save_every == 0:
            save_future = await training_client.save_state_async(f"step-{step + 1:05d}")
            save_result = await save_future.result_async()
            logger.info("Checkpoint saved: %s", save_result.path)

    logger.info("Training complete. Results: %s", results_dir)
    return step_results
