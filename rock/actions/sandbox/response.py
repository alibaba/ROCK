from typing import Annotated, Literal

from pydantic import BaseModel, Field

import rock


class SandboxResult(BaseModel):
    code: rock.codes | None = None
    # 向前兼容
    exit_code: int | None = None
    failure_reason: str | None = None
    message: str = ""


class IsAliveResponse(SandboxResult):
    is_alive: bool

    def __bool__(self) -> bool:
        return self.is_alive


class SandboxStatusResponse(SandboxResult):
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
    cpus: float | None = None
    memory: str | None = None


class CommandResponse(SandboxResult):
    stdout: str = ""
    stderr: str = ""


class WriteFileResponse(SandboxResult):
    success: bool = False


class OssSetupResponse(SandboxResult):
    success: bool = False


class ExecuteBashSessionResponse(SandboxResult):
    success: bool = False


class CreateBashSessionResponse(SandboxResult):
    output: str = ""

    session_type: Literal["bash"] = "bash"


CreateSessionResponse = Annotated[CreateBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all create session responses. Do not use this directly."""


class BashObservation(SandboxResult):
    session_type: Literal["bash"] = "bash"
    output: str = ""
    expect_string: str = ""


Observation = BashObservation


class CloseBashSessionResponse(SandboxResult):
    session_type: Literal["bash"] = "bash"


CloseSessionResponse = Annotated[CloseBashSessionResponse, Field(discriminator="session_type")]
"""Union type for all close session responses. Do not use this directly."""


class ReadFileResponse(SandboxResult):
    content: str = ""
    """Content of the file as a string."""


class UploadResponse(SandboxResult):
    success: bool = False
    file_name: str = ""


FileUploadResponse = UploadResponse


class CloseResponse(SandboxResult):
    """Response for close operations."""

    pass
