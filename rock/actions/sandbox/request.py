from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Command(BaseModel):
    session_type: Literal["bash"] = "bash"
    command: str | list[str]

    timeout: float | None = 1200
    """The timeout for the command. None means no timeout."""

    env: dict[str, str] | None = None
    """Environment variables to pass to the command."""

    cwd: str | None = None
    """The current working directory to run the command in."""


class CreateBashSessionRequest(BaseModel):
    session_type: Literal["bash"] = "bash"
    session: str = "default"
    startup_source: list[str] = []
    env_enable: bool = False
    env: dict[str, str] | None = Field(default=None)
    remote_user: str | None = Field(default=None)


CreateSessionRequest = Annotated[CreateBashSessionRequest, Field(discriminator="session_type")]
"""Union type for all create session requests. Do not use this directly."""


class BashAction(BaseModel):
    action_type: Literal["bash"] = "bash"
    command: str
    session: str = "default"
    timeout: float | None = None
    check: Literal["silent", "raise", "ignore"] = "raise"


Action = Annotated[BashAction, Field(discriminator="action_type")]


class WriteFileRequest(BaseModel):
    content: str
    path: str


class CloseBashSessionRequest(BaseModel):
    session_type: Literal["bash"] = "bash"
    session: str = "default"


CloseSessionRequest = Annotated[CloseBashSessionRequest, Field(discriminator="session_type")]
"""Union type for all close session requests. Do not use this directly."""


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
