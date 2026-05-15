"""Rocklet — abstract base class for local sandbox runtimes.

Concrete subclasses live in:
- rock.rocklet.linux:   LinuxRocklet  (Linux/macOS, BashSession via pexpect)
- rock.rocklet.windows: WindowsRocklet (Windows, PowerShellSession via subprocess)

Use Rocklet.create(**kwargs) to obtain the right subclass for the current OS.
"""

import asyncio
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rock.actions import (
    AbstractSandbox,
    CloseResponse,
    CloseSessionResponse,
    CommandResponse,
    CreateSessionResponse,
    EnvCloseResponse,
    EnvListResponse,
    EnvMakeResponse,
    EnvResetResponse,
    EnvStepResponse,
    IsAliveResponse,
    Observation,
    ReadFileRequest,
    ReadFileResponse,
    RockletConfig,
    UploadRequest,
    UploadResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseSessionRequest as CloseSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.logger import init_logger
from rock.rocklet.exceptions import (
    CommandTimeoutError,
    NonZeroExitCodeError,
    SessionDoesNotExistError,
    SessionExistsError,
    UnsupportedPlatformError,
)

logger = init_logger(__name__)


class Session(ABC):
    """Abstract command session inside a sandbox.

    Concrete implementations: BashSession (Linux/macOS), PowerShellSession (Windows).

    Signatures intentionally use the wide discriminated types
    (Action / Observation / CreateSessionResponse / CloseSessionResponse) to
    keep the contract identical to the original Session ABC.
    """

    @abstractmethod
    async def start(self) -> CreateSessionResponse:
        ...

    @abstractmethod
    async def run(self, action: Action) -> Observation:
        ...

    @abstractmethod
    async def close(self) -> CloseSessionResponse:
        ...


class Rocklet(AbstractSandbox, ABC):
    """Abstract base for local sandbox runtimes."""

    def __init__(self, *, executor: ThreadPoolExecutor | None = None, **kwargs: Any):
        """A Runtime that runs locally and actually executes commands in a shell.
        If you are deploying to Modal/Fargate/etc., this class will be running within the docker container
        on Modal/Fargate/etc.

        Args:
            **kwargs: Keyword arguments (see `RockletConfig` for details).
        """
        self._config = RockletConfig(**kwargs)
        self._sessions: dict[str, Session] = {}
        # Set up logger
        self.command_logger = init_logger("command", "command.log")
        self._executor = executor
        self._gem_envs: dict[str, Any] = {}

    @classmethod
    def create(cls, **kwargs: Any) -> "Rocklet":
        """Construct the Rocklet subclass for the current OS.

        Lazy-imports the platform-specific module so that, e.g., pexpect is never
        loaded on Windows and the Windows-only module is never executed on Linux.

        Raises:
            UnsupportedPlatformError: if sys.platform has no Rocklet subclass.
        """
        match sys.platform:
            case "linux" | "darwin":
                from rock.rocklet.linux import LinuxRocklet

                return LinuxRocklet(**kwargs)
            case "win32":
                from rock.rocklet.windows import WindowsRocklet

                return WindowsRocklet(**kwargs)
            case other:
                raise UnsupportedPlatformError(f"No Rocklet subclass registered for sys.platform={other!r}")

    @classmethod
    def from_config(cls, config: RockletConfig) -> "Rocklet":
        return cls.create(**config.model_dump())

    @abstractmethod
    def _build_bash_session(self, request: CreateBashSessionRequest) -> Session:
        """Construct the platform's default bash-style session."""

    @abstractmethod
    async def get_statistics(self) -> dict:
        """Return CPU / memory / disk / net usage as a dict."""

    @property
    def sessions(self) -> dict[str, Session]:
        return self._sessions

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive."""
        return IsAliveResponse(is_alive=True)

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        """Creates a new session.

        Delegates to the subclass's `_build_bash_session`, which selects
        BashSession on Linux/macOS and PowerShellSession on Windows.
        """
        if request.session in self.sessions:
            msg = f"session {request.session} already exists"
            raise SessionExistsError(msg)
        if isinstance(request, CreateBashSessionRequest):
            session = self._build_bash_session(request)
        else:
            msg = f"unknown session type: {request!r}"
            raise ValueError(msg)
        self.sessions[request.session] = session
        self.command_logger.info(f"[create_session]:{request.session}")
        return await session.start()

    async def run_in_session(self, action: Action) -> Observation:
        """Runs a command in a session."""
        if action.session not in self.sessions:
            msg = f"session {action.session!r} does not exist"
            raise SessionDoesNotExistError(msg)
        self.command_logger.info(f"[run_in_session input][{action.session}]:{action.command}")
        observation = await self.sessions[action.session].run(action)
        if observation.output:
            self.command_logger.info(f"[run_in_session output][{action.session}]:{observation.output}")
        if observation.exit_code:
            self.command_logger.info(f"[run_in_session exit_code][{action.session}]:{observation.exit_code}")
        if observation.failure_reason:
            self.command_logger.info(f"[run_in_session failure_reason][{action.session}]:{observation.failure_reason}")
        return observation

    async def close_session(self, request: CloseSessionRequest) -> CloseSessionResponse:
        """Closes a shell session."""
        if request.session not in self.sessions:
            msg = f"session {request.session!r} does not exist"
            raise SessionDoesNotExistError(msg)
        out = await self.sessions[request.session].close()
        del self.sessions[request.session]
        self.command_logger.info(f"[close_session]:{request.session}")
        return out

    async def execute(self, command: Command) -> CommandResponse:
        """Executes a command (independent of any shell session).

        Raises:
            CommandTimeoutError: If the command times out.
            NonZeroExitCodeError: If the command has a non-zero exit code and `check` is True.
        """
        self.command_logger.info(f"[execute input]:{command.command}")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, self._run_subprocess_blocking, command)
            r = CommandResponse(
                stdout=result.stdout.decode(errors="backslashreplace"),
                stderr=result.stderr.decode(errors="backslashreplace"),
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"Timeout ({command.timeout}s) exceeded while running command"
            raise CommandTimeoutError(msg) from e
        if command.check and result.returncode != 0:
            msg = (
                f"Command {command.command!r} failed with exit code {result.returncode}. "
                f"Stdout:\n{r.stdout!r}\nStderr:\n{r.stderr!r}"
            )
            if command.error_msg:
                msg = f"{command.error_msg}: {msg}"
            raise NonZeroExitCodeError(msg)
        if r.stdout:
            self.command_logger.info(f"[execute stdout]:{r.stdout}")
        if r.stderr:
            self.command_logger.info(f"[execute stderr]:{r.stderr}")
        return r

    def _run_subprocess_blocking(self, command: Command):
        # This is synchronous blocking code
        return subprocess.run(
            command.command,
            shell=command.shell,
            timeout=command.timeout,
            env=command.env,
            capture_output=True,
            cwd=command.cwd,
        )

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        """Reads a file"""
        self.command_logger.info(f"[read_file input]: {request.path}")
        content = Path(request.path).read_text(encoding=request.encoding, errors=request.errors)
        self.command_logger.info(f"[read_file output]: {content[:1000]}")
        return ReadFileResponse(content=content)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        """Writes a file"""
        self.command_logger.info(f"[write_file input]: {request.path}")
        self.command_logger.info(f"[write_file content]: {request.content[:1000]}")
        Path(request.path).parent.mkdir(parents=True, exist_ok=True)
        Path(request.path).write_text(request.content)
        return WriteFileResponse(success=True)

    async def upload(self, request: UploadRequest) -> UploadResponse:
        """Uploads a file"""
        self.command_logger.info(f"[upload source]: {request.source_path}")
        self.command_logger.info(f"[upload target]: {request.target_path}")
        if Path(request.source_path).is_dir():
            shutil.copytree(request.source_path, request.target_path)
        else:
            shutil.copy(request.source_path, request.target_path)
        self.command_logger.info("[upload output]: upload success!")
        return UploadResponse()

    async def close(self) -> CloseResponse:
        """Closes the runtime."""
        for session in self.sessions.values():
            await session.close()
        return CloseResponse()

    def env_make(self, env_id: str, sandbox_id: str) -> EnvMakeResponse:
        """Make gem env"""
        import gem

        env = gem.make(env_id)
        self._gem_envs[sandbox_id] = env
        return EnvMakeResponse(sandbox_id=sandbox_id)

    def env_step(self, sandbox_id: str, action: str) -> EnvStepResponse:
        """Step gem env"""
        env = self._gem_envs[sandbox_id]
        observation, reward, terminated, truncated, info = env.step(action)
        return EnvStepResponse(
            observation=observation,
            reward=reward,
            terminated=terminated,
            truncated=truncated,
            info=info,
        )

    def env_reset(self, sandbox_id: str, seed: int | None = None) -> EnvResetResponse:
        """Reset gem env"""
        env = self._gem_envs[sandbox_id]
        observation, info = env.reset(seed=seed)
        return EnvResetResponse(observation=observation, info=info)

    def env_close(self, sandbox_id: str) -> EnvCloseResponse:
        """Close gem env"""
        del self._gem_envs[sandbox_id]
        return EnvCloseResponse(sandbox_id=sandbox_id)

    def env_list(self) -> EnvListResponse:
        """List gem env"""
        from gem.envs.registration import ENV_REGISTRY

        return EnvListResponse(env_id=list(ENV_REGISTRY))
