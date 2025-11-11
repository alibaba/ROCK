from typing import Annotated

from pydantic import Field

from rock.actions import (
    BashAction,
    CloseBashSessionRequest,
    Command,
    CreateBashSessionRequest,
    ReadFileRequest,
    WriteFileRequest,
)


class InternalCommand(Command):
    container_name: str | None = None


class InternalCreateBashSessionRequest(CreateBashSessionRequest):
    container_name: str | None = None


InternalCreateSessionRequest = Annotated[InternalCreateBashSessionRequest, Field(discriminator="session_type")]


class InternalBashAction(BashAction):
    container_name: str | None = None


InternalAction = InternalBashAction


class InternalCloseBashSessionRequest(CloseBashSessionRequest):
    container_name: str | None = None


InternalCloseSessionRequest = Annotated[InternalCloseBashSessionRequest, Field(discriminator="session_type")]


class InternalReadFileRequest(ReadFileRequest):
    container_name: str | None = None


class InternalWriteFileRequest(WriteFileRequest):
    container_name: str | None = None
