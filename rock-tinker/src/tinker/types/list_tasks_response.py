from __future__ import annotations

from pydantic import Field

from .._models import BaseModel
from .task_descriptor import TaskDescriptor

__all__ = ["ListTasksResponse"]


class ListTasksResponse(BaseModel):
    tasks: list[TaskDescriptor] = Field(default_factory=list)
    total: int = 0
