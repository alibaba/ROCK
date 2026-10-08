from typing import Any, List, Optional

from .._models import BaseModel
from .stop_reason import StopReason

__all__ = ["SampledSequence"]


class SampledSequence(BaseModel):
    stop_reason: StopReason
    """Reason why sampling stopped"""

    tokens: List[int]
    """List of generated token IDs"""

    logprobs: Optional[List[float]] = None
    """Log probabilities for each token (optional)"""

    text: Optional[str] = None
    """Parsed generated text returned by a backend runtime."""

    raw_text: Optional[str] = None
    """Generated text before runtime tool-call parsing, when available."""

    tool_calls: Optional[List[dict[str, Any]]] = None
    """OpenAI-compatible tool calls parsed by the backend runtime."""

    finish_reason: Optional[str] = None
    """Runtime completion reason, including tool_calls when applicable."""
