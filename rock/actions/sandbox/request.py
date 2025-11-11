from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Command(BaseModel):
    """A command to run as a subprocess."""

    session_type: Literal["bash"] = "bash"

    command: str | list[str]
    """The command to run. Should be a list of strings (recommended because
    of automatic escaping of spaces etc.) unless you set `shell=True`
    (i.e., exactly like with `subprocess.run()`).
    """

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


class CreateBashSessionRequest(BaseModel):
    session: str = "default"
    session_type: Literal["bash"] = "bash"
    startup_source: list[str] = []
    startup_timeout: float = 1.0
    """The timeout for the startup commands."""
    max_read_size: int = 2000
    env_enable: bool = False
    env: dict[str, str] | None = Field(default=None)


CreateSessionRequest = Annotated[CreateBashSessionRequest, Field(discriminator="session_type")]
"""Union type for all create session requests. Do not use this directly."""


class BashAction(BaseModel):
    action_type: Literal["bash"] = "bash"
    command: str
    session: str = "default"
    check: Literal["silent", "raise", "ignore"] = "raise"

    timeout: float | None = None
    """The timeout (seconds) for the command. None means no timeout."""

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


class BashInterruptAction(BaseModel):
    command: str = "interrupt"

    session: str = "default"

    timeout: float = 0.2
    """The timeout for the command. None means no timeout."""

    n_retry: int = 3
    """How many times to retry quitting."""

    expect: list[str] = []
    """Outputs to expect in addition to the PS1"""

    action_type: Literal["bash_interrupt"] = "bash_interrupt"


Action = BashAction


class WriteFileRequest(BaseModel):
    content: str
    path: str


class CloseBashSessionRequest(BaseModel):
    session: str = "default"
    session_type: Literal["bash"] = "bash"


CloseSessionRequest = Annotated[CloseBashSessionRequest, Field(discriminator="session_type")]
"""Union type for all close session requests. Do not use this directly."""


class InitDockerEnvRequest(BaseModel):
    image: str = ""
    """Docker image name to use for the container."""

    python_standalone_dir: str | None = None
    """Directory path for the Python standalone installation."""

    auto_clear_time: int = 60 * 6
    """Automatic container cleanup time in minutes."""

    pull: Literal["never", "always", "missing"] = "missing"
    """Docker image pull policy: 'never', 'always', or 'missing'."""

    memory: str = "8g"
    """Memory allocation for the container (e.g., '8g', '4096m')."""

    cpus: float = 2
    """Number of CPU cores to allocate for the container."""

    container_name: str | None = None
    """Custom name for the container. If None, a random name will be generated."""


class ReadFileRequest(BaseModel):
    path: str
    """File path to read from."""

    encoding: str | None = None
    """Text encoding to use when reading the file. None uses default encoding.
    This corresponds to the `encoding` parameter of `Path.read_text()`."""

    errors: str | None = None
    """Error handling strategy when reading the file. None uses default handling.
    This corresponds to the `errors` parameter of `Path.read_text()`."""


class UploadRequest(BaseModel):
    source_path: str
    """Local file path to upload from."""

    target_path: str
    """Remote file path to upload to."""
