"""
Run SWE-Bench tasks through the full step loop concurrently.

Usage:
    TINKER_API_KEY=tml-test python3 tinker_cookbook/rock_harbor_bench/eval_swe_bench_concurrent.py /cpfs04/user/wuhaotian.wht/data/SWE-Env-one.jsonl

The input file may contain repeated copies of the same task. Each row is
initialized as a separate remote task environment and run in parallel up to
--concurrency.
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


def build_config(args: argparse.Namespace) -> Any:
    from tinker_cookbook.eval import EvalConfig

    return EvalConfig(
        model_name="Qwen/Qwen3-4B-Instruct-2507",
        tokenizer_path="/root/.cache/modelscope/hub/models/Qwen/Qwen3-4B-Instruct-2507",
        renderer_name="qwen3",
        max_turns=args.max_turns,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        dataset_name="SWE-Env/SWE-Env",
        dataset_type="v2_2023pr",
        base_url=args.base_url,
    )


async def run_concurrent_tasks(
    config: Any,
    task_infos: list[dict[str, Any]],
    concurrency: int,
) -> None:
    from tinker_cookbook.rock_harbor_bench.eval_swe_bench import (
        _safe_path_part,
        run_one_task,
    )

    results_root = Path(config.output_path) / f"concurrent_tasks_full_{datetime.now():%Y%m%d_%H%M%S}"
    results_root.mkdir(parents=True, exist_ok=True)
    print(f"Results root: {results_root}")
    print(f"Running {len(task_infos)} task row(s) with concurrency={concurrency}")

    semaphore = asyncio.Semaphore(concurrency)
    aggregate_lock = asyncio.Lock()
    summaries: list[dict | None] = [None] * len(task_infos)
    aggregate_path = results_root / "aggregate_summary.json"
    started_at = time.monotonic()

    async def write_aggregate() -> None:
        completed = [item for item in summaries if item is not None]
        aggregate = {
            "tasks_total": len(task_infos),
            "tasks_completed": len(completed),
            "tasks_with_errors": sum(1 for item in completed if item.get("error")),
            "tasks_episode_done": sum(1 for item in completed if item.get("episode_done")),
            "reward_sum": sum(float(item.get("reward", 0.0)) for item in completed),
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
            "concurrency": concurrency,
            "results": completed,
        }
        aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))

    async def run_indexed_task(task_idx: int, task_info: dict[str, Any]) -> None:
        task_name = task_info.get("task_name") or task_info["instance_id"]
        task_dir = results_root / f"{task_idx:03d}_{_safe_path_part(task_name)}"

        async with semaphore:
            print(f"===== concurrent task {task_idx + 1}/{len(task_infos)} start: {task_name} =====")
            try:
                summary = await run_one_task(config, task_info, results_dir=task_dir)
            except Exception as e:
                summary = {
                    "task_index": task_idx,
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
                print(f"Concurrent task {task_idx + 1} failed: {e!r}")
            else:
                summary["task_index"] = task_idx
            finally:
                summaries[task_idx] = summary
                async with aggregate_lock:
                    await write_aggregate()
                print(f"===== concurrent task {task_idx + 1}/{len(task_infos)} done: {task_name} =====")

    await asyncio.gather(
        *(run_indexed_task(task_idx, task_info) for task_idx, task_info in enumerate(task_infos))
    )
    print(f"All concurrent tasks finished. Aggregate summary: {aggregate_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run JSONL SWE tasks concurrently through the full Tinker step loop."
    )
    parser.add_argument("data_path", help="Path to a JSON or JSONL task file")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Maximum number of task rows to run concurrently",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--max-turns", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    return args


def main() -> None:
    args = parse_args()
    from tinker_cookbook.rock_harbor_bench.env import load_harbor_tasks

    config = build_config(args)
    task_infos = load_harbor_tasks(args.data_path)
    if not task_infos:
        raise ValueError(f"No tasks found in {args.data_path}")
    asyncio.run(run_concurrent_tasks(config, task_infos, args.concurrency))


if __name__ == "__main__":
    main()
