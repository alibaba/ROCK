from typing import Annotated, Literal, TypedDict

from fastapi import Header
from pydantic import BaseModel, Field

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
    sandbox_id: str | None = Field(default=None)
    """The id of the sandbox."""
    registry_username: str | None = None
    """Username for Docker registry authentication. When both username and password are provided, docker login will be performed before pulling the image."""
    registry_password: str | None = None
    """Password for Docker registry authentication. When both username and password are provided, docker login will be performed before pulling the image."""


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


class SandboxCreateBashSessionRequest(CreateBashSessionRequest):
    startup_timeout: float = 1.0
    max_read_size: int = 2000
    sandbox_id: str | None = None
    remote_user: str | None = Field(default=None)


SandboxCreateSessionRequest = Annotated[SandboxCreateBashSessionRequest, Field(discriminator="session_type")]


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


SandboxAction = Annotated[SandboxBashAction, Field(discriminator="action_type")]


class SandboxCloseBashSessionRequest(CloseBashSessionRequest):
    sandbox_id: str | None = None


SandboxCloseSessionRequest = Annotated[SandboxCloseBashSessionRequest, Field(discriminator="session_type")]


class SandboxReadFileRequest(ReadFileRequest):
    sandbox_id: str | None = None


class SandboxWriteFileRequest(WriteFileRequest):
    sandbox_id: str | None = None


class WarmupRequest(BaseModel):
    image: str = "python:3.11"


class BatchSandboxStatusRequest(BaseModel):
    sandbox_ids: list[str]


class SandboxQueryParams(TypedDict, total=False):
    """Sandbox列表查询参数"""

    page: str
    page_size: str
    user_id: str
    experiment_id: str
    namespace: str
    image: str
    state: str


class UserInfo(TypedDict, total=False):
    user_id: str
    experiment_id: str
    namespace: str
    rock_authorization: str


class ClusterInfo(TypedDict, total=False):
    cluster_name: str


class StartHeaders:
    def __init__(
        self,
        x_user_id: str | None = Header(default="default", alias="X-User-Id"),
        x_experiment_id: str | None = Header(default="default", alias="X-Experiment-Id"),
        rock_authorization: str | None = Header(default="default", alias="X-Key"),
        x_namespace: str | None = Header(default="default", alias="X-Namespace"),
        x_cluster: str | None = Header(default="default", alias="X-Cluster"),
    ):
        self.user_info: UserInfo = {
            "user_id": x_user_id,
            "experiment_id": x_experiment_id,
            "namespace": x_namespace,
            "rock_authorization": rock_authorization,
        }
        self.cluster_info: ClusterInfo = {
            "cluster_name": x_cluster,
        }
