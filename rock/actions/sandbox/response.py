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


class SystemResourceMetrics(BaseModel):
    """System resource metrics"""

    total_cpu: float = 0.0
    """Total CPU cores"""

    total_memory: float = 0.0
    """Total memory in GB"""

    available_cpu: float = 0.0
    """Available CPU cores"""

    available_memory: float = 0.0
    """Available memory in GB"""

    gpu_count: int = 0
    """Total GPU count"""

    available_gpu: int = 0
    """Available GPU count"""

    def get_cpu_utilization(self) -> float:
        """Get CPU utilization rate (0.0 - 1.0)"""
        if self.total_cpu == 0:
            return 0.0
        return (self.total_cpu - self.available_cpu) / self.total_cpu

    def get_memory_utilization(self) -> float:
        """Get memory utilization rate (0.0 - 1.0)"""
        if self.total_memory == 0:
            return 0.0
        return (self.total_memory - self.available_memory) / self.total_memory

    def get_gpu_utilization(self) -> float:
        """Get GPU utilization rate (0.0 - 1.0)"""
        if self.gpu_count == 0:
            return 0.0
        return (self.gpu_count - self.available_gpu) / self.gpu_count
