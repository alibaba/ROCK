from __future__ import annotations

from .._models import StrictBase
from .dataset_spec import DatasetSpec

__all__ = ["ListTasksRequest"]


class ListTasksRequest(StrictBase):
    datasets: list[DatasetSpec]
