from __future__ import annotations

from typing import Any

from .._models import StrictBase

__all__ = ["InitTaskEnvRequest"]


class InitTaskEnvRequest(StrictBase):
    """Request to initialize a task environment."""

    task_id: str
    """Task identifier."""

    dataset: str
    """Dataset name."""

    split: str
    """Dataset split."""

    metadata: dict[str, Any] | None = None
    """Optional task metadata passed through to the backend."""
