from __future__ import annotations

from .._models import StrictBase

__all__ = ["DatasetSpec"]


class DatasetSpec(StrictBase):
    dataset: str
    split: str
    bench_name: str
    task_filter: str | None = None
