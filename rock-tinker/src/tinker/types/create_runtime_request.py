from __future__ import annotations

from typing import Any

from typing_extensions import Literal

from .._models import StrictBase

__all__ = ["CreateRuntimeRequest"]


class CreateRuntimeRequest(StrictBase):
    runtime_type: str
    config_type: str = "yaml"
    config_content: str
    session_id: str | None = None
    user_metadata: dict[str, Any] | None = None

    type: Literal["create_runtime"] = "create_runtime"
