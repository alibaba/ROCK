# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
from typing import Optional

from ..._models import BaseModel
from ..model_id import ModelID
from ..request_id import RequestID

__all__ = ["UntypedAPIFuture"]


class UntypedAPIFuture(BaseModel):
    request_id: RequestID

    future_id: RequestID | None = None
    runtime_id: str | None = None
    status: str | None = None
    type: str | None = None

    model_id: Optional[ModelID] = None
