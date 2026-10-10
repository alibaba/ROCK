from __future__ import annotations

from typing_extensions import Literal

from .._models import BaseModel

__all__ = ["CreateRuntimeResponse"]


class CreateRuntimeResponse(BaseModel):
    runtime_id: str
    runtime_type: str
    status: str
    ready: bool
    config_type: str
    config_path: str | None = None
    adapter_base_url: str | None = None
    error_message: str | None = None

    type: Literal["create_runtime"] = "create_runtime"
