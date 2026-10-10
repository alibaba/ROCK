from enum import Enum

__all__ = ["FinishReason"]


class FinishReason(str, Enum):
    """Reason why a task environment episode ended."""

    FINISH = "finish"
    """Normal completion."""

    EXCEED_MAX_STEP = "exceed_max_step"
    """Exceeded the maximum allowed steps."""

    EXIT = "exit"
    """Agent explicitly requested to exit."""
