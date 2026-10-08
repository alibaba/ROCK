from typing import Optional

from .._models import StrictBase

__all__ = ["CloseRuntimeRequest"]


class CloseRuntimeRequest(StrictBase):
    """Request to close a backend runtime."""

    force_after_seconds: Optional[float] = None
    """Seconds to wait for graceful runtime shutdown before backend force cleanup."""

    type: str = "close_runtime"
