from typing import Protocol

from fastapi import UploadFile

from rock.actions import CommandResponse, ReadFileResponse, UploadResponse, WriteFileResponse
from rock.admin.proto.request import SandboxCommand, SandboxReadFileRequest, SandboxWriteFileRequest

ROCKLET_BACKEND = "rocklet"
OPENSANDBOX_BACKEND = "opensandbox"


class SandboxRuntimeBackend(Protocol):
    async def execute(self, sandbox_id: str, info: dict, command: SandboxCommand) -> CommandResponse: ...

    async def read_file(self, sandbox_id: str, info: dict, request: SandboxReadFileRequest) -> ReadFileResponse: ...

    async def write_file(self, sandbox_id: str, info: dict, request: SandboxWriteFileRequest) -> WriteFileResponse: ...

    async def upload(self, sandbox_id: str, info: dict, file: UploadFile, target_path: str) -> UploadResponse: ...
