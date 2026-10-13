from typing import Optional

from .._models import BaseModel
from .finish_reason import FinishReason
from .model_input import ModelInput

__all__ = ["GetStepResponse"]


class GetStepResponse(BaseModel):
    """Response from getting a step for a task environment."""

    step_id: int
    """Step number that was retrieved."""

    prompt: Optional[ModelInput] = None
    """Prompt at this step as ModelInput."""

    finish_reason: Optional[FinishReason] = None
    """Reason why the episode ended (if finished)."""

    reward: Optional[float] = None
    """Reward signal for this step."""

    warning_message: Optional[str] = None
    """Non-fatal warning attached to a terminal step."""
