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
