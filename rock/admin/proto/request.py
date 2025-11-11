from typing import Literal

from pydantic import BaseModel

from rock import env_vars
from rock.actions import (
    BashAction,
    CloseBashSessionRequest,
    Command,
    CreateBashSessionRequest,
    InitDockerEnvRequest,
    ReadFileRequest,
    WriteFileRequest,
)
from rock.rocklet.proto.request import (
    InternalBashAction,
    InternalCloseBashSessionRequest,
    InternalCommand,
    InternalCreateBashSessionRequest,
    InternalReadFileRequest,
    InternalWriteFileRequest,
)


class SandboxStartRequest(BaseModel):
    image: str = ""
    """image"""
    auto_clear_time_minutes: int = env_vars.ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES
    """The time for automatic container cleaning, with the unit being minutes"""
    pull: Literal["never", "always", "missing"] = "missing"
    """When to pull docker images."""
    memory: str = "8g"
    """The amount of memory to allocate for the container."""
    cpus: float = 2
    """The amount of CPUs to allocate for the container."""

    def transform(self) -> InitDockerEnvRequest:
        res = InitDockerEnvRequest(**self.model_dump())
        res.auto_clear_time = self.auto_clear_time_minutes
        return res


class SandboxCommand(Command):
    sandbox_id: str | None = None

    def transform(self) -> InternalCommand:
        res = InternalCommand(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxCreateBashSessionRequest(CreateBashSessionRequest):
    sandbox_id: str | None = None

    def transform(self) -> InternalCreateBashSessionRequest:
        res = InternalCreateBashSessionRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxBashAction(BashAction):
    sandbox_id: str | None = None

    def transform(self) -> InternalBashAction:
        res = InternalBashAction(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxCloseBashSessionRequest(CloseBashSessionRequest):
    sandbox_id: str | None = None

    def transform(self) -> InternalCloseBashSessionRequest:
        res = InternalCloseBashSessionRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        res.session_type = "bash"
        return res


class SandboxReadFileRequest(ReadFileRequest):
    sandbox_id: str | None = None

    def transform(self) -> InternalReadFileRequest:
        res = InternalReadFileRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxWriteFileRequest(WriteFileRequest):
    sandbox_id: str | None = None

    def transform(self) -> InternalWriteFileRequest:
        res = InternalWriteFileRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class WarmupRequest(BaseModel):
    image: str = "hub.docker.alibaba-inc.com/chatos/python:3.11"
