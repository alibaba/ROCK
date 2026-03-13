from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from rock._codes import codes


class SandboxResponse(BaseModel):
    code: codes | None = None
    failure_reason: str | None = None


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"


class IsAliveResponse(BaseModel):
    """Response to the is_alive request.

    You can test the result with bool().
    """

    is_alive: bool

    message: str = ""
    """Error message if is_alive is False."""

    def __bool__(self) -> bool:
        return self.is_alive


class CommandResponse(SandboxResponse):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class WriteFileResponse(SandboxResponse):
    success: bool = False
    message: str = ""


class OssSetupResponse(BaseModel):
    success: bool = False
    message: str = ""


class ExecuteBashSessionResponse(BaseModel):
    success: bool = False
    message: str = ""


class CreateBashSessionResponse(SandboxResponse):
    output: str = ""

    session_type: Literal["bash"] = "bash"


CreateSessionResponse = Annotated[CreateBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all create session responses. Do not use this directly."""


class BashObservation(SandboxResponse):
    exit_code: int | None = None
    session_type: Literal["bash"] = "bash"
    output: str = ""
    expect_string: str = ""


Observation = BashObservation


class CloseBashSessionResponse(SandboxResponse):
    session_type: Literal["bash"] = "bash"


CloseSessionResponse = Annotated[CloseBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all close session responses. Do not use this directly."""


class ReadFileResponse(SandboxResponse):
    content: str = ""
    """Content of the file as a string."""


class UploadResponse(SandboxResponse):
    success: bool = False
    message: str = ""
    file_name: str = ""


FileUploadResponse = UploadResponse


class CloseResponse(SandboxResponse):
    """Response for close operations."""

    pass
