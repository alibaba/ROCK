import re
import shlex
from io import IOBase
from pathlib import Path

from fastapi import UploadFile

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.response import State
from rock.admin.proto.request import (
    SandboxBashAction,
    SandboxCommand,
    SandboxCreateBashSessionRequest,
    SandboxReadFileRequest,
    SandboxWriteFileRequest,
)
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

    @staticmethod
    def _execution_output(execution) -> tuple[str, str]:
        stdout = "".join(message.text for message in execution.logs.stdout)
        stderr = "".join(message.text for message in execution.logs.stderr)
        if execution.error:
            stderr = f"{stderr}\n{execution.error}" if stderr else str(execution.error)
        return stdout, stderr

    async def create_session(
        self,
        sandbox_id: str,
        info: dict,
        request: SandboxCreateBashSessionRequest,
    ) -> tuple[str, CreateBashSessionResponse]:
        opensandbox_id = self._opensandbox_id(info)
        if request.remote_user is not None:
            execution = await self._client.execute(opensandbox_id, "id -un")
            stdout, stderr = self._execution_output(execution)
            effective_user = stdout.strip()
            if execution.exit_code != 0 or not effective_user:
                detail = stderr.strip() or f"exit code {execution.exit_code}"
                raise BadRequestRockError(f"Cannot determine OpenSandbox effective user: {detail}")
            if request.remote_user != effective_user:
                raise BadRequestRockError(
                    f"OpenSandbox does not support remote_user={request.remote_user}; "
                    f"the sandbox effective user is {effective_user}"
                )

        # Execd sessions inherit the sandbox/container environment. `env_enable`
        # must not copy the Admin process environment across the trust boundary.
        init_commands = []
        for key, value in (request.env or {}).items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                raise BadRequestRockError(f"Invalid environment variable name: {key!r}")
            init_commands.append(f"export {key}={shlex.quote(value)}")
        init_commands.extend(f"source {shlex.quote(path)}" for path in request.startup_source)

        session_id = await self._client.create_session(opensandbox_id)
        if not init_commands:
            return session_id, CreateBashSessionResponse()

        try:
            execution = await self._client.run_in_session(
                opensandbox_id,
                session_id,
                " && ".join(init_commands),
                timeout=request.startup_timeout,
            )
            stdout, stderr = self._execution_output(execution)
            if execution.error and "timeout" in execution.error.name.lower():
                raise CommandTimeoutError(
                    f"Timeout ({request.startup_timeout}s) exceeded while initializing OpenSandbox session"
                )
            if execution.exit_code != 0:
                raise NonZeroExitCodeError(
                    f"Failed to initialize OpenSandbox session with exit code {execution.exit_code}: "
                    f"{(stderr or stdout)!r}"
                )
            return session_id, CreateBashSessionResponse(output=stdout + stderr)
        except Exception:
            try:
                await self._client.delete_session(opensandbox_id, session_id)
            except Exception as cleanup_error:
                logger.warning(
                    "[%s] failed to clean up OpenSandbox session after initialization: %s", sandbox_id, cleanup_error
                )
            raise

    async def run_session(
        self,
        sandbox_id: str,
        info: dict,
        session_id: str,
        action: SandboxBashAction,
    ) -> BashObservation:
        if action.is_interactive_command or action.is_interactive_quit or action.expect:
            raise BadRequestRockError("OpenSandbox sessions do not support interactive commands")
        execution = await self._client.run_in_session(
            self._opensandbox_id(info),
            session_id,
            action.command,
            timeout=action.timeout,
        )
        stdout, stderr = self._execution_output(execution)
        if execution.error and "timeout" in execution.error.name.lower():
            raise CommandTimeoutError(f"Timeout ({action.timeout}s) exceeded while running session command")
        if action.check == "raise" and execution.exit_code != 0:
            message = (
                f"Command {action.command!r} failed with exit code {execution.exit_code}. "
                f"Here is the output:\n{(stdout + stderr)!r}"
            )
            if action.error_msg:
                message = f"{action.error_msg}: {message}"
            raise NonZeroExitCodeError(message)
        return BashObservation(
            output=stdout + stderr,
            exit_code=None if action.check == "ignore" else execution.exit_code,
        )

    async def close_session(self, sandbox_id: str, info: dict, session_id: str) -> CloseBashSessionResponse:
        del sandbox_id
        await self._client.delete_session(self._opensandbox_id(info), session_id)
        return CloseBashSessionResponse()

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
