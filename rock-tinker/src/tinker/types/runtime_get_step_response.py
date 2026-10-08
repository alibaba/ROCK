from typing import Optional

from .._models import BaseModel
from .finish_reason import FinishReason
from .openai_chat_prompt import OpenAIChatPrompt

__all__ = ["RuntimeGetStepResponse"]


class RuntimeGetStepResponse(BaseModel):
    """A task step returned by an independent backend runtime."""

    step_id: int
    prompt: Optional[OpenAIChatPrompt] = None
    finish_reason: Optional[FinishReason] = None
    reward: Optional[float] = None
    warning_message: Optional[str] = None


