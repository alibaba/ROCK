from typing import Literal

from pydantic import BaseModel

from rock import env_vars
from rock.actions import (
    BashAction,
    CloseBashSessionRequest,
    Command,
    CreateBashSessionRequest,
    ReadFileRequest,
    WriteFileRequest,
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

    def transform(self):
        from rock.deployments.config import DockerDeploymentConfig

        res = DockerDeploymentConfig(**self.model_dump())
        return res


class SandboxCommand(Command):
    timeout: float | None = 1200
    """The timeout for the command. None means no timeout."""
    shell: bool = False
    """Same as the `subprocess.run()` `shell` argument."""
    check: bool = False
    """Whether to check for the exit code. If True, we will raise a
    `CommandFailedError` if the command fails.
    """
    error_msg: str = ""
    """This error message will be used in the `NonZeroExitCodeError` if the
    command has a non-zero exit code and `check` is True.
    """
    env: dict[str, str] | None = None
    """Environment variables to pass to the command."""
    cwd: str | None = None
    """The current working directory to run the command in."""
    sandbox_id: str | None = None
    """The id of the sandbox."""

    def transform(self):
        from rock.rocklet.proto.request import InternalCommand

        res = InternalCommand(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxCreateBashSessionRequest(CreateBashSessionRequest):
    startup_timeout: float = 1.0
    max_read_size: int = 2000
    sandbox_id: str | None = None

    def transform(self):
        from rock.rocklet.proto.request import InternalCreateBashSessionRequest

        res = InternalCreateBashSessionRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxBashAction(BashAction):
    sandbox_id: str | None = None
    """The id of the sandbox."""
    is_interactive_command: bool = False
    """For a non-exiting command to an interactive program
    (e.g., gdb), set this to True."""
    is_interactive_quit: bool = False
    """This will disable checking for exit codes, since the command won't terminate.
    If the command is something like "quit" and should terminate the
    interactive program, set this to False.
    """
    error_msg: str = ""
    """This error message will be used in the `NonZeroExitCodeError` if the
    command has a non-zero exit code and `check` is True.
    """
    expect: list[str] = []
    """Outputs to expect in addition to the PS1"""

    def transform(self):
        from rock.rocklet.proto.request import InternalBashAction

        res = InternalBashAction(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxCloseBashSessionRequest(CloseBashSessionRequest):
    sandbox_id: str | None = None

    def transform(self):
        from rock.rocklet.proto.request import InternalCloseBashSessionRequest

        res = InternalCloseBashSessionRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxReadFileRequest(ReadFileRequest):
    sandbox_id: str | None = None

    def transform(self):
        from rock.rocklet.proto.request import InternalReadFileRequest

        res = InternalReadFileRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class SandboxWriteFileRequest(WriteFileRequest):
    sandbox_id: str | None = None

    def transform(self):
        from rock.rocklet.proto.request import InternalWriteFileRequest

        res = InternalWriteFileRequest(**self.model_dump())
        res.container_name = self.sandbox_id
        return res


class WarmupRequest(BaseModel):
    image: str = "python:3.11"
