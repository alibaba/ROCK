from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from rock._codes import codes


class SandboxResponse(BaseModel):
    code: codes | None = None
    exit_code: int | None = None
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


class SandboxStatusResponse(BaseModel):
    sandbox_id: str = None
    status: dict = None
    port_mapping: dict = None
    host_name: str | None = None
    host_ip: str | None = None
    is_alive: bool = True
    image: str | None = None
    gateway_version: str | None = None
    swe_rex_version: str | None = None
    user_id: str | None = None
    experiment_id: str | None = None
    namespace: str | None = None
    cpus: float | None = None
    memory: str | None = None


class CommandResponse(BaseModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


class WriteFileResponse(BaseModel):
    success: bool = False
    message: str = ""


class OssSetupResponse(BaseModel):
    success: bool = False
    message: str = ""


class ExecuteBashSessionResponse(BaseModel):
    success: bool = False
    message: str = ""


class CreateBashSessionResponse(BaseModel):
    output: str = ""

    session_type: Literal["bash"] = "bash"


CreateSessionResponse = Annotated[CreateBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all create session responses. Do not use this directly."""


class BashObservation(BaseModel):
    session_type: Literal["bash"] = "bash"
    output: str = ""
    exit_code: int | None = None
    failure_reason: str = ""
    expect_string: str = ""


Observation = BashObservation


class CloseBashSessionResponse(BaseModel):
    session_type: Literal["bash"] = "bash"


CloseSessionResponse = Annotated[CloseBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all close session responses. Do not use this directly."""


class ReadFileResponse(BaseModel):
    content: str = ""
    """Content of the file as a string."""


class UploadResponse(BaseModel):
    success: bool = False
    message: str = ""
    file_name: str = ""


FileUploadResponse = UploadResponse


class CloseResponse(BaseModel):
    """Response for close operations."""

    pass


class ChownResponse(BaseModel):
    success: bool = False
    message: str = ""


class ChmodResponse(BaseModel):
    success: bool = False
    message: str = ""
