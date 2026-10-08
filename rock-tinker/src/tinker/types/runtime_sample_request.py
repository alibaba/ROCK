from typing import Optional

from pydantic import model_validator
from typing_extensions import Literal

from .._compat import PYDANTIC_V2, ConfigDict
from .._models import StrictBase
from .model_input import ModelInput
from .openai_chat_prompt import OpenAIChatPrompt
from .sampling_params import SamplingParams

__all__ = ["RuntimeSampleRequest"]


class RuntimeSampleRequest(StrictBase):
    """Sample request for an independent backend runtime.

    Chat rollout requests use ``prompt``. Low-level token operations such as
    reference scoring use ``model_input``. The two representations are never
    overloaded in one field.
    """

    num_samples: int = 1
    prompt: Optional[OpenAIChatPrompt] = None
    model_input: Optional[ModelInput] = None
    sampling_params: SamplingParams
    base_model: Optional[str] = None
    model_path: Optional[str] = None
    sampling_session_id: Optional[str] = None
    env_id: Optional[str] = None
    seq_id: Optional[int] = None
    prompt_logprobs: Optional[bool] = None
    topk_prompt_logprobs: int = 0
    type: Literal["sample"] = "sample"

    @model_validator(mode="after")
    def validate_exactly_one_input(self) -> "RuntimeSampleRequest":
        if (self.prompt is None) == (self.model_input is None):
            raise ValueError("Exactly one of prompt or model_input must be provided")
        return self

    if PYDANTIC_V2:
        model_config = ConfigDict(protected_namespaces=tuple(), frozen=True, extra="forbid")


