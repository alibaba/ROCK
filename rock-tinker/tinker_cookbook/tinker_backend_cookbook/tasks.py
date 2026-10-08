from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import tinker

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_BENCH_NAME = "SWE-bench"
DEFAULT_TASK_ID = "sympy__sympy-19637"


def add_task_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--bench-name", default=DEFAULT_BENCH_NAME)
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument("--task-filter", default=None)
    parser.add_argument("--task-seed", type=int, default=None)



def task_filter_from_args(args: argparse.Namespace) -> str | None:
    if args.task_filter:
        return str(args.task_filter)
    if args.task_id:
        return f"^{re.escape(str(args.task_id))}$"
    return None


async def load_tasks(
    args: argparse.Namespace,
) -> list[tinker.TaskDescriptor]:
    if args.task_id and not args.task_filter:
        return [
            tinker.TaskDescriptor(
                task_id=str(args.task_id),
                dataset=str(args.dataset),
                split=str(args.split),
                bench_name=str(args.bench_name),
            )
        ]

    catalog = tinker.PublicTaskCatalog()
    tasks = await catalog.select_tasks(
        dataset=str(args.dataset),
        split=str(args.split),
        bench_name=str(args.bench_name),
        task_filter=task_filter_from_args(args),
        seed=getattr(args, "task_seed", None),
    )
    if not tasks:
        raise ValueError(
            "No public tasks found for "
            f"dataset={args.dataset!r} split={args.split!r} "
            f"bench_name={args.bench_name!r} task_filter={task_filter_from_args(args)!r}"
        )
    return tasks


async def load_one_task(
    args: argparse.Namespace,
) -> tinker.TaskDescriptor:
    return (await load_tasks(args))[0]


def write_task_manifest(
    output_dir: str | Path,
    tasks: list[tinker.TaskDescriptor],
    args: argparse.Namespace,
) -> Path:
    output_path = Path(output_dir) / "task_manifest.json"
    payload = {
        "version": 1,
        "dataset": str(args.dataset),
        "split": str(args.split),
        "bench_name": str(args.bench_name),
        "selection": {
            "task_id": str(args.task_id) if args.task_id and not args.task_filter else None,
            "task_filter": str(args.task_filter) if args.task_filter else None,
            "seed": getattr(args, "task_seed", None),
        },
        "tasks": [task.model_dump(mode="json") for task in tasks],
    }
    temporary = output_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def task_name(task: tinker.TaskDescriptor | Any) -> str:
    return str(getattr(task, "task_id", task))
