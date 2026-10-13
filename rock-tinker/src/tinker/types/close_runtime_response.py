from typing import Optional

from typing_extensions import Literal
from pydantic import Field

from .._models import BaseModel

__all__ = ["CloseRuntimeResponse"]


class CloseRuntimeResponse(BaseModel):
    """Response returned after a backend runtime has been closed."""

    runtime_id: str
    status: str
    graceful: bool
    killed_pids: list[int] = Field(default_factory=list)
    message: Optional[str] = None

    type: Literal["close_runtime"] = "close_runtime"
