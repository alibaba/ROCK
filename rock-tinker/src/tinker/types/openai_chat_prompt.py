from typing import Any

from pydantic import field_validator
from typing_extensions import Literal

from .._models import StrictBase

__all__ = ["OpenAIChatPrompt"]


class OpenAIChatPrompt(StrictBase):
    """A human-readable OpenAI chat request owned by a model runtime."""

    type: Literal["openai_chat_completion_request"] = "openai_chat_completion_request"
    version: Literal[1] = 1
    request: dict[str, Any]

    @field_validator("request")
    @classmethod
    def validate_openai_chat_request(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("messages"), list):
            raise ValueError("OpenAI chat prompt request must include a messages list")
        return value
