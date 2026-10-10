from typing import Optional

from .._models import StrictBase

__all__ = ["GetStepRequest"]


class GetStepRequest(StrictBase):
    """Request to get a specific step for a task environment."""

    env_id: str
    """Environment identifier"""

    step_id: Optional[int] = None
    """Step number to retrieve (None means latest/current)"""
