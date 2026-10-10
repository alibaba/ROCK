"""Public dataset task discovery for local Tinker clients."""
from __future__ import annotations
import random
import re
from ._utils._sync import to_thread
from .types import TaskDescriptor

__all__ = ["PublicTaskCatalog"]


def list_public_task_ids(dataset: str, split: str) -> list[str]:
    """Read public instance IDs without an OSS registry or credentials."""
    if not dataset or not split:
        raise ValueError("dataset and split must not be empty")
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise ImportError("Task filtering requires datasets; use the local control environment or install tinker[task-catalog].") from exc
    rows = load_dataset(dataset, split=split, streaming=True)
    task_ids: set[str] = set()
    for row in rows:
        task_id = row.get("instance_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Public task datasets must provide a non-empty instance_id column")
        task_ids.add(task_id)
    return sorted(task_ids)


class PublicTaskCatalog:
    """List and select descriptors from public Hugging Face datasets."""

    async def list_tasks(self, *, dataset: str, split: str, bench_name: str) -> list[TaskDescriptor]:
        task_ids = await to_thread(list_public_task_ids, dataset, split)
        return [TaskDescriptor(task_id=x, dataset=dataset, split=split, bench_name=bench_name) for x in task_ids]

    async def select_tasks(
        self, *, dataset: str, split: str, bench_name: str,
        task_filter: str | None = None, limit: int | None = None, seed: int | None = None,
    ) -> list[TaskDescriptor]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        pattern = re.compile(task_filter) if task_filter else None
        tasks = await self.list_tasks(dataset=dataset, split=split, bench_name=bench_name)
        if pattern:
            tasks = [task for task in tasks if pattern.search(task.task_id)]
        if seed is not None:
            random.Random(seed).shuffle(tasks)
        return tasks[:limit] if limit is not None else tasks
