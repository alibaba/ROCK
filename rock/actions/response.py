from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel

from rock._codes import codes


class ResponseStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"
    code: codes | None = None


class BaseResponse(BaseModel):
    status: ResponseStatus = ResponseStatus.SUCCESS
    message: str | None = None
    error: str | None = None


T = TypeVar("T")


class RockResponse(BaseResponse, Generic[T]):
    result: T | None = None
