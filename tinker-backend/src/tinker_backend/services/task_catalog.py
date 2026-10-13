"""Public dataset task discovery."""

from __future__ import annotations

import re
from collections.abc import Iterable

from tinker.task_catalog import list_public_task_ids
from tinker_backend.protocol.schemas import DatasetSpec, TaskDescriptor


def list_task_ids(dataset: str, split: str) -> list[str]:
    return list_public_task_ids(dataset, split)


def list_task_descriptors(datasets: Iterable[DatasetSpec]) -> list[TaskDescriptor]:
    results: list[TaskDescriptor] = []
    for dataset_spec in datasets:
        task_ids = list_task_ids(
            dataset_spec.dataset,
            dataset_spec.split,
        )
        if dataset_spec.task_filter:
            pattern = re.compile(dataset_spec.task_filter)
            task_ids = [task_id for task_id in task_ids if pattern.search(task_id)]
        for task_id in task_ids:
            results.append(
                TaskDescriptor(
                    task_id=task_id,
                    dataset=dataset_spec.dataset,
                    split=dataset_spec.split,
                    bench_name=dataset_spec.bench_name,
                )
            )
    return results
