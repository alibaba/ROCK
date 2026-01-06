from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel

import rock


class ResponseStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"


class BaseResponse(BaseModel):
    status: ResponseStatus = ResponseStatus.SUCCESS
    message: str | None = None
    error: str | None = None
    code: rock.codes | None = None


T = TypeVar("T")


class RockResponse(BaseResponse, Generic[T]):
    result: T | None = None
