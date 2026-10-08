"""Run one public dataset task through the Tinker backend runtime path."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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

os.environ.setdefault("TINKER_LOG", "info")

import tinker  # noqa: E402
from tinker_cookbook.rl.rollouts import do_single_rollout  # noqa: E402
from tinker_cookbook.tinker_backend_cookbook.completer import RuntimePromptCompleter  # noqa: E402
from tinker_cookbook.tinker_backend_cookbook.env import RemoteSandboxEnv  # noqa: E402
from tinker_cookbook.tinker_backend_cookbook.server_job_marker import emit_server_job_marker
from tinker_cookbook.tinker_backend_cookbook.tasks import (  # noqa: E402
    add_task_args,
    load_one_task,
    task_name,
    write_task_manifest,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "tinker_backend_cookbook" / "roll_runtime.yaml"
logger = logging.getLogger(__name__)


def _configure_cookbook_logging() -> None:
    level_name = os.environ.get("TINKER_LOG", "info").lower()
    level = logging.DEBUG if level_name == "debug" else logging.INFO
    logging.getLogger().setLevel(level)


_configure_cookbook_logging()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one task through Tinker backend runtime")
    add_task_args(parser)
    parser.add_argument("--runtime-type", default="ROLL")
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--output-path", default=str(Path(__file__).with_name("results")))
    parser.add_argument("--max-turns", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--api-key", default=os.environ.get("TINKER_API_KEY", "tml-dummy"))
    return parser.parse_args()


async def run_one_rollout(args: argparse.Namespace) -> dict[str, Any]:
    output_root = Path(args.output_path) / f"runtime_rollout_{datetime.now():%Y%m%d_%H%M%S}"
    output_root.mkdir(parents=True, exist_ok=True)
    task = await load_one_task(args)
    write_task_manifest(output_root, [task], args)
    logger.info(
        "Selected local public dataset task task_id=%s dataset=%s split=%s bench=%s output_root=%s",
        task.task_id,
        task.dataset,
        task.split,
        task.bench_name,
        output_root,
    )

    logger.info("Opening ServerJob from runtime YAML config_path=%s", args.config_path)
    started_at = time.monotonic()
    async with tinker.ServerJob.open(config_path=args.config_path, api_key=args.api_key) as job:
        logger.info("ServerJob ready job_id=%s backend_base_url=%s", job.job_id, job.base_url)
        emit_server_job_marker(job)
        client = await job.get_client()
        logger.info("Creating runtime runtime_type=%s config_path=%s", args.runtime_type, args.config_path)
        runtime = await client.create_runtime(
            runtime_type=args.runtime_type,
            config_path=args.config_path,
        )
        logger.info(
            "Runtime ready runtime_id=%s runtime_type=%s status=%s ready=%s elapsed=%.1fs",
            runtime.runtime_id,
            runtime.runtime_type,
            runtime.status,
            runtime.ready,
            time.monotonic() - started_at,
        )
        async with runtime:
            env = RemoteSandboxEnv(
                task=task,
                runtime=runtime,
                max_turns=args.max_turns,
            )
            policy = RuntimePromptCompleter(
                sampling_client=runtime,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
            logger.info("Starting rollout task=%s max_turns=%s", task_name(task), args.max_turns)
            trajectory = await do_single_rollout(policy, env)
            elapsed = time.monotonic() - started_at
            reward = sum(t.reward for t in trajectory.transitions)
            logger.info("Rollout complete turns=%s reward=%s elapsed=%.1fs", len(trajectory.transitions), reward, elapsed)
            summary = {
                "mode": "runtime",
                "runtime_id": runtime.runtime_id,
                "runtime_type": runtime.runtime_type,
                "runtime_ready": runtime.ready,
                "runtime_status": runtime.status,
                "env_id": env.get_env_id(),
                "task_name": task_name(task),
                "task_id": task.task_id,
                "dataset": task.dataset,
                "split": task.split,
                "bench_name": task.bench_name,
                "turns_used": len(trajectory.transitions),
                "reward": reward,
                "episode_done": bool(trajectory.transitions and trajectory.transitions[-1].episode_done),
                "time_seconds": round(elapsed, 3),
                "sample_tokens": list(trajectory.transitions[0].ac.tokens) if trajectory.transitions else [],
            }

    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results dir: {output_root}")
    print(f"Runtime: {summary['runtime_id']} ({summary['runtime_status']})")
    print(f"Env: {summary['env_id']}")
    print(f"Task: {summary['task_name']}")
    print(f"Summary: {json.dumps(summary, ensure_ascii=False)}")
    return summary


def main() -> None:
    asyncio.run(run_one_rollout(parse_args()))


if __name__ == "__main__":
    main()
