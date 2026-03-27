from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnvMakeResponse(BaseModel):
    sandbox_id: str


class EnvResetResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    observation: Any
    info: dict = Field(default_factory=dict)


class EnvStepResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    observation: Any
    reward: float
    terminated: bool
    truncated: bool
    info: dict = Field(default_factory=dict)


class EnvCloseResponse(BaseModel):
    sandbox_id: str


class EnvListResponse(BaseModel):
    env_id: list[str]
