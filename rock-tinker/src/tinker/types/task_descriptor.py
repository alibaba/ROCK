from __future__ import annotations

from typing import Any

from pydantic import Field

from .._models import BaseModel

__all__ = ["TaskDescriptor"]


class TaskDescriptor(BaseModel):
    task_id: str
    dataset: str
    split: str
    bench_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
