import shlex
from io import IOBase
from pathlib import Path

from fastapi import UploadFile

from rock.actions import CommandResponse, ReadFileResponse, UploadResponse, WriteFileResponse
from rock.actions.sandbox.response import State
from rock.admin.proto.request import SandboxCommand, SandboxReadFileRequest, SandboxWriteFileRequest
from rock.logger import init_logger
from rock.rocklet.exceptions import CommandTimeoutError, NonZeroExitCodeError
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)


class _BinaryStreamAdapter(IOBase):
    """Expose UploadFile's file-like stream as the IOBase expected by the SDK."""

    def __init__(self, stream):
        self._stream = stream

    def read(self, size=-1):
        return self._stream.read(size)

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def seekable(self):
        return self._stream.seekable()


class OpenSandboxBackend:
    def __init__(self, client: OpenSandboxClient):
        self._client = client

    @staticmethod
    def _opensandbox_id(info: dict) -> str:
        opensandbox_id = (info.get("extended_params") or {}).get("opensandbox_id")
        if not opensandbox_id:
            raise BadRequestRockError("OpenSandbox sandbox metadata is missing opensandbox_id")
        return opensandbox_id

    async def execute(self, sandbox_id: str, info: dict, command: SandboxCommand) -> CommandResponse:
        opensandbox_id = self._opensandbox_id(info)
        command_text = shlex.join(command.command) if isinstance(command.command, list) else command.command
        if isinstance(command.command, str) and not command.shell:
            logger.warning(
                "[%s] OpenSandbox executes string commands with shell semantics although shell=False",
                sandbox_id,
            )
        execution = await self._client.execute(
            opensandbox_id,
            command_text,
            timeout=command.timeout,
            cwd=command.cwd,
            env=command.env,
        )
        if execution.error and "timeout" in execution.error.name.lower():
            raise CommandTimeoutError(f"Timeout ({command.timeout}s) exceeded while running command")
        stdout = "".join(message.text for message in execution.logs.stdout)
        stderr = "".join(message.text for message in execution.logs.stderr)
        if execution.error:
            stderr = f"{stderr}\n{execution.error}" if stderr else str(execution.error)
        response = CommandResponse(stdout=stdout, stderr=stderr, exit_code=execution.exit_code)
        if command.check and execution.exit_code != 0:
            message = (
                f"Command failed with exit code {execution.exit_code}. "
                f"Stdout:\n{response.stdout!r}\nStderr:\n{response.stderr!r}"
            )
            if command.error_msg:
                message = f"{command.error_msg}: {message}"
            raise NonZeroExitCodeError(message)
        return response

    async def get_state(self, info: dict) -> State:
        remote_state = await self._client.get_state(self._opensandbox_id(info))
        return {
            "Pending": State.PENDING,
            "Running": State.RUNNING,
            "Pausing": State.STOPPED,
            "Paused": State.STOPPED,
            "Stopping": State.STOPPED,
            "Terminated": State.DELETED,
            "Failed": State.STOPPED,
        }.get(remote_state, State.PENDING)

    async def get_endpoint(self, sandbox_id: str, info: dict, port: int):
        del sandbox_id
        return await self._client.get_endpoint(self._opensandbox_id(info), port)

    async def read_file(self, sandbox_id: str, info: dict, request: SandboxReadFileRequest) -> ReadFileResponse:
        opensandbox_id = self._opensandbox_id(info)
        content = await self._client.read_bytes(opensandbox_id, request.path)
        return ReadFileResponse(
            content=content.decode(
                encoding=request.encoding or "utf-8",
                errors=request.errors or "strict",
            )
        )

    async def _file_mode(self, opensandbox_id: str, path: str) -> int:
        info_by_path = await self._client.get_file_info(opensandbox_id, path)
        existing = info_by_path.get(path)
        return existing.mode if existing is not None else 644

    async def write_file(self, sandbox_id: str, info: dict, request: SandboxWriteFileRequest) -> WriteFileResponse:
        opensandbox_id = self._opensandbox_id(info)
        mode = await self._file_mode(opensandbox_id, request.path)
        await self._client.write_file(opensandbox_id, request.path, request.content, mode=mode)
        return WriteFileResponse(success=True)

    async def upload(self, sandbox_id: str, info: dict, file: UploadFile, target_path: str) -> UploadResponse:
        opensandbox_id = self._opensandbox_id(info)
        mode = await self._file_mode(opensandbox_id, target_path)
        stream = file.file if isinstance(file.file, IOBase) else _BinaryStreamAdapter(file.file)
        await self._client.write_file(opensandbox_id, target_path, stream, mode=mode)
        return UploadResponse(success=True, file_name=Path(target_path).name)

    async def aclose(self) -> None:
        await self._client.aclose()
