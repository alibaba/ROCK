from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from rock import codes


class ResponseStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"


class BaseResponse(BaseModel):
    status: ResponseStatus = ResponseStatus.SUCCESS
    message: str | None = None
    error: str | None = None
    code: codes | None = None


T = TypeVar("T")


class RockResponse(BaseResponse, Generic[T]):
    result: T | None = None
