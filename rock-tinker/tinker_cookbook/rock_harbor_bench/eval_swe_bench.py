"""
Run SWE-Bench tasks through the full step loop.

The full original example is backed up as:
    tinker_cookbook/rock_harbor_bench/eval_swe_bench_full.py

Usage:
    TINKER_API_KEY=tml-test python3 tinker_cookbook/rock_harbor_bench/eval_swe_bench.py /cpfs04/user/wuhaotian.wht/data/SWE-Env-five.jsonl

This debug entrypoint runs every task from the input file sequentially until
episode completion or max_turns. It uses the normal token-id path: sample
returns token ids, the SDK-side tokenizer decodes those ids, and env.step()
advances with the same token ids.
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

import tinker

from tinker_cookbook.completers import TinkerTokenCompleter
from tinker_cookbook.eval import EvalConfig
from tinker_cookbook.renderers import get_renderer
from tinker_cookbook.rl.types import ActionExtra, Trajectory, Transition
from tinker_cookbook.rock_harbor_bench.env import (
    RemoteSandboxEnv,
    TaskInfo,
    load_harbor_tasks,
)
from tinker_cookbook.utils.display import format_trajectory
from tinker_cookbook.utils import model_info, tokenizer_utils


def _safe_path_part(value: str) -> str:
    return "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in value)


async def run_one_task(
    config: EvalConfig,
    task_info: TaskInfo,
    results_dir: Path | None = None,
) -> dict:
    if results_dir is None:
        results_dir = Path(config.output_path) / f"one_task_full_{datetime.now():%Y%m%d_%H%M%S}"
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results dir: {results_dir}")

    tinker_client = tinker.TinkerClient(base_url=config.base_url)
    if config.checkpoint_url:
        sampling_client = tinker_client.create_sampling_client(
            model_path=config.checkpoint_url,
            base_model=config.model_name,
        )
    else:
        sampling_client = tinker_client.create_sampling_client(base_model=config.model_name)

    tokenizer = tokenizer_utils.get_tokenizer(config.tokenizer_path or config.model_name)
    renderer_name = config.renderer_name or model_info.get_recommended_renderer_name(
        config.model_name
    )
    renderer = get_renderer(renderer_name, tokenizer)
    policy = TinkerTokenCompleter(
        sampling_client=sampling_client,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )

    task_name = task_info.get("task_name") or task_info["instance_id"]
    dataset_name = task_info.get("dataset_name") or config.dataset_name
    dataset_type = task_info.get("dataset_type") or config.dataset_type
    remote_task_info: TaskInfo = {
        "task_name": task_name,
        "instance_id": task_name,
        "dataset_name": dataset_name,
        "dataset_type": dataset_type,
    }
    env = RemoteSandboxEnv(
        task_info=remote_task_info,
        sampling_client=sampling_client,
        renderer=renderer,
        max_turns=config.max_turns,
    )

    started_at = time.monotonic()
    ob, stop_condition = await env.get_observation(init=True)
    env_id = env.get_env_id()
    print(f"Task: {task_name}")
    print(f"Dataset: {dataset_name} ({dataset_type})")
    print(f"Env id: {env_id}")
    print(f"Initial prompt tokens: {ob.length}")
    print(f"Max turns: {config.max_turns}")

    transitions: list[Transition] = []
    turn_records: list[dict] = []

    for turn_idx in range(config.max_turns):
        turn_start = time.monotonic()
        action = await policy(ob, stop_condition, env_id=env_id)
        assistant_text = renderer.tokenizer.decode(action.tokens)
        sample_elapsed = time.monotonic() - turn_start

        print(f"----- turn {turn_idx} assistant text begin -----")
        print(assistant_text)
        print(f"----- turn {turn_idx} assistant text end -----")
        print(
            f"Turn {turn_idx}: prompt_tokens={ob.length}, "
            f"assistant_tokens={len(action.tokens)}, "
            f"stop_reason={action.stop_reason}, "
            f"sample_time={sample_elapsed:.1f}s"
        )

        step_result = await env.step(
            action.tokens,
            extra=ActionExtra(stop_reason=action.stop_reason),
        )
        transitions.append(
            Transition(
                ob=ob,
                ac=action,
                reward=step_result.reward,
                episode_done=step_result.episode_done,
                metrics=step_result.metrics,
                logs=step_result.logs,
            )
        )
        turn_records.append({
            "turn": turn_idx,
            "prompt_tokens": ob.length,
            "assistant_tokens": len(action.tokens),
            "stop_reason": action.stop_reason,
            "sample_seconds": round(sample_elapsed, 3),
            "reward": step_result.reward,
            "episode_done": step_result.episode_done,
            "metrics": step_result.metrics,
            "logs": step_result.logs,
            "assistant_text": assistant_text,
            "tokens": action.tokens,
        })
        (results_dir / f"assistant_turn_{turn_idx:03d}.txt").write_text(assistant_text)

        print(
            f"Turn {turn_idx} env result: "
            f"episode_done={step_result.episode_done}, "
            f"reward={step_result.reward}, "
            f"metrics={step_result.metrics}, "
            f"logs={step_result.logs}"
        )

        ob = step_result.next_observation
        if step_result.episode_done:
            break
        stop_condition = step_result.next_stop_condition
    else:
        print(f"Reached max_turns={config.max_turns} without terminal episode_done.")

    elapsed = time.monotonic() - started_at
    trajectory = Trajectory(transitions=transitions, final_ob=ob)
    reward = sum(t.reward for t in transitions)
    summary = {
        "task_name": task_name,
        "dataset_name": dataset_name,
        "dataset_type": dataset_type,
        "env_id": env_id,
        "turns_used": len(transitions),
        "reward": reward,
        "time_seconds": round(elapsed, 1),
        "episode_done": transitions[-1].episode_done if transitions else False,
        "final_metrics": transitions[-1].metrics if transitions else {},
        "final_logs": transitions[-1].logs if transitions else {},
    }

    (results_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    (results_dir / "trajectory.json").write_text(
        json.dumps({**summary, "turns": turn_records}, ensure_ascii=False, indent=2)
    )
    (results_dir / "trajectory.txt").write_text(
        format_trajectory(trajectory, tokenizer, only_last_transition=False)
    )
    print(f"Final summary: {summary}")
    return summary


async def run_all_tasks(config: EvalConfig, task_infos: list[TaskInfo]) -> None:
    results_root = Path(config.output_path) / f"all_tasks_full_{datetime.now():%Y%m%d_%H%M%S}"
    results_root.mkdir(parents=True, exist_ok=True)
    print(f"Results root: {results_root}")

    summaries: list[dict] = []
    aggregate_path = results_root / "aggregate_summary.json"

    for task_idx, task_info in enumerate(task_infos):
        task_name = task_info.get("task_name") or task_info["instance_id"]
        task_dir = results_root / f"{task_idx:03d}_{_safe_path_part(task_name)}"
        print(f"===== task {task_idx + 1}/{len(task_infos)}: {task_name} =====")

        try:
            summary = await run_one_task(config, task_info, results_dir=task_dir)
        except Exception as e:
            summary = {
                "task_name": task_name,
                "dataset_name": task_info.get("dataset_name") or config.dataset_name,
                "dataset_type": task_info.get("dataset_type") or config.dataset_type,
                "reward": 0.0,
                "turns_used": 0,
                "time_seconds": 0.0,
                "episode_done": False,
                "error": repr(e),
            }
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "error.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2)
            )
            print(f"Task {task_name} failed: {e!r}")

        summaries.append(summary)
        aggregate = {
            "tasks_total": len(task_infos),
            "tasks_completed": len(summaries),
            "tasks_with_errors": sum(1 for item in summaries if item.get("error")),
            "tasks_episode_done": sum(1 for item in summaries if item.get("episode_done")),
            "reward_sum": sum(float(item.get("reward", 0.0)) for item in summaries),
            "results": summaries,
        }
        aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))
        print(f"Aggregate summary updated: {aggregate_path}")

    print(f"All tasks finished. Aggregate summary: {aggregate_path}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 tinker_cookbook/rock_harbor_bench/eval_swe_bench.py <data_path>")
        sys.exit(1)

    data_path = sys.argv[1]
    config = EvalConfig(
        model_name="Qwen/Qwen3-4B-Instruct-2507",
        tokenizer_path="/root/.cache/modelscope/hub/models/Qwen/Qwen3-4B-Instruct-2507",
        renderer_name="qwen3",
        max_turns=200,
        temperature=0.1,
        max_tokens=8192,
        dataset_name="SWE-Env/SWE-Env",
        dataset_type="v2_2023pr",
        base_url="http://127.0.0.1:9000",
    )
    task_infos = load_harbor_tasks(data_path)
    if not task_infos:
        raise ValueError(f"No tasks found in {data_path}")
    print(f"Loaded {len(task_infos)} task(s), running all tasks sequentially through all steps.")
    asyncio.run(run_all_tasks(config, task_infos))


if __name__ == "__main__":
    main()
