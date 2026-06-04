from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel

from rock._codes import codes


class ResponseStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"


class BaseResponse(BaseModel):
    status: ResponseStatus = ResponseStatus.SUCCESS
    message: str | None = None
    error: str | None = None
    code: codes | None = None
    """Structured error code on the envelope. Preferred over reading
    ``result.code`` (the legacy ``SandboxResponse`` payload), which is kept
    populated only for backward-compat on endpoints whose ``T`` is a
    ``SandboxResponse`` subclass."""


T = TypeVar("T")


class RockResponse(BaseResponse, Generic[T]):
    result: T | None = None
