from __future__ import annotations

import shlex
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, NewType

from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.utils import arun_with_retry, with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import RunModeType, Sandbox
    from rock.sdk.sandbox.runtime_env.config import RuntimeEnvConfig

logger = init_logger(__name__)

RuntimeEnvId = NewType("RuntimeEnvId", str)


class RuntimeEnv(ABC):
    """Runtime environment (e.g., Python/Node).

    Each RuntimeEnv is identified by (type, version) tuple and is managed by Sandbox.runtime_envs.
    workdir is auto-generated as: /tmp/rock-runtime-envs/{type}/{version}/{runtime_env_id}
    session is auto-generated as: runtime-env-{type}-{version}-{runtime_env_id}

    Usage:
        env = await RuntimeEnv.create(sandbox, config)
        await env.run("python --version")
    """

    # Registry for subclasses (auto-registered by __init_subclass__)
    _REGISTRY: dict[str, type[RuntimeEnv]] = {}

    # Runtime type discriminator
    runtime_env_type: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Register subclass based on its runtime_env_type property
        # The subclass must define runtime_env_type as a class attribute
        if hasattr(cls, "runtime_env_type") and isinstance(cls.runtime_env_type, str):
            cls._REGISTRY[cls.runtime_env_type] = cls

    @classmethod
    async def create(cls, sandbox: Sandbox, runtime_env_config: RuntimeEnvConfig) -> RuntimeEnv:
        """Async factory method: create RuntimeEnv from config and initialize it.

        This creates a RuntimeEnv instance of the appropriate type and initializes it.
        The instance is automatically registered to sandbox.runtime_envs.

        Args:
            sandbox: Sandbox instance
            runtime_env_config: Runtime environment configuration

        Returns:
            Initialized RuntimeEnv instance of the appropriate type
        """
        runtime_type = runtime_env_config.type
        runtime_class = cls._REGISTRY.get(runtime_type)
        if runtime_class is None:
            raise ValueError(f"Unsupported runtime type: {runtime_type}")
        runtime_env = runtime_class(sandbox=sandbox, runtime_env_config=runtime_env_config)
        # Auto-register to sandbox.runtime_envs
        sandbox.runtime_envs[runtime_env._runtime_env_id] = runtime_env
        await runtime_env.init()
        return runtime_env

    def __init__(
        self,
        sandbox: Sandbox,
        runtime_env_config: RuntimeEnvConfig,
    ) -> None:
        self._sandbox = sandbox

        # Extract values from config
        self._version = runtime_env_config.version
        self._env = runtime_env_config.env
        self._install_timeout = runtime_env_config.install_timeout
        self._custom_install_cmd = runtime_env_config.custom_install_cmd
        self._extra_symlink_dir = runtime_env_config.extra_symlink_dir
        self._extra_symlink_executables = runtime_env_config.extra_symlink_executables

        # Unique ID for this runtime env instance

        self._runtime_env_id = RuntimeEnvId(str(uuid.uuid4())[:8])

        version_str = self._version or "default"  # avoid version is ""
        self._workdir = f"/tmp/rock-runtime-envs/{runtime_env_config.type}/{version_str}/{self._runtime_env_id}"
        self._session = f"runtime-env-{runtime_env_config.type}-{version_str}-{self._runtime_env_id}"

        # State flag
        self._initialized: bool = False
        self._session_ready: bool = False

    @property
    def initialized(self) -> bool:
        """Whether the runtime has been initialized."""
        return self._initialized

    @property
    def runtime_env_id(self) -> RuntimeEnvId:
        """Unique ID for this runtime env instance."""
        return self._runtime_env_id

    @property
    def workdir(self) -> str:
        """Working directory for this runtime env instance."""
        return self._workdir

    @property
    def bin_dir(self) -> str:
        """Binary directory for this runtime env instance."""
        return f"{self.workdir}/runtime-env/bin"

    async def init(self) -> None:
        """Initialize the runtime environment.

        This method performs installation and validation.
        It is idempotent: calling multiple times only initializes once.
        Subclasses should override _post_init() for additional initialization.
        """
        if self._initialized:
            return

        # Common setup: ensure session and workdir
        await self._ensure_session()
        await self._ensure_workdir()

        # Install runtime and then do additional initialization
        await self._install_runtime()
        await self._post_init()

        # Execute custom install command after _post_init
        if self._custom_install_cmd:
            await self._do_custom_install()

        # Create symlinks for executables
        await self._create_sys_path_links()

        self._initialized = True

    async def run(
        self,
        cmd: str,
        mode: RunModeType | None = None,
        wait_timeout: int = 600,
        error_msg: str = "runtime env command failed",
    ):
        """Run a command under this runtime"""

        from rock.sdk.sandbox.client import RunMode

        if mode is None:
            mode = RunMode.NOHUP

        await self._ensure_session()
        wrapped = self.wrapped_cmd(cmd, prepend=True)

        logger.debug(f"[{self._sandbox.sandbox_id}] RuntimeEnv run cmd: {wrapped}")

        result = await self._sandbox.arun(
            cmd=wrapped,
            session=self._session,
            mode=mode,
            wait_timeout=wait_timeout,
        )
        # If exit_code is not 0, raise an exception to trigger retry
        if result.exit_code != 0:
            raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")
        return result

    def wrapped_cmd(self, cmd: str, prepend: bool = True) -> str:
        """Always wrap with bash -c to ensure it only affects current cmd. Default prepend=True to give current runtime_env highest priority."""

        if prepend:
            wrapped = f"export PATH={shlex.quote(self.bin_dir)}:$PATH && {cmd}"
        else:
            wrapped = f"export PATH=$PATH:{shlex.quote(self.bin_dir)} && {cmd}"
        return f"bash -c {shlex.quote(wrapped)}"

    async def _ensure_session(self) -> None:
        """Ensure runtime env session exists. Safe to call multiple times."""
        if self._session_ready:
            return

        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self._session,
                env_enable=True,
                env=self._env,
            )
        )
        self._session_ready = True

    async def _ensure_workdir(self) -> None:
        """Create workdir for runtime environment."""
        result = await self._sandbox.arun(
            cmd=f"mkdir -p {self._workdir}",
            session=self._session,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to create workdir: {self._workdir}, exit_code: {result.exit_code}")

    @abstractmethod
    def _get_install_cmd(self) -> str:
        """Get installation command for this runtime environment (e.g., 'python-install.sh')."""
        pass

    @with_time_logging("Installing runtime")
    async def _install_runtime(self) -> None:
        """Install the runtime environment."""
        from rock.sdk.sandbox.client import RunMode

        install_cmd = f"cd {shlex.quote(self._workdir)} && {self._get_install_cmd()}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self._session,
            mode=RunMode.NOHUP,
            wait_timeout=self._install_timeout,
            error_msg=f"{self.runtime_env_type} runtime installation failed",
        )

    async def _post_init(self) -> None:
        """Additional initialization after runtime installation. Override in subclasses."""
        pass

    @with_time_logging("Running custom install")
    async def _do_custom_install(self) -> None:
        """Execute custom install command after _post_init."""
        await self.run(
            f"cd {shlex.quote(self._workdir)} && {self._custom_install_cmd}",
            wait_timeout=self._install_timeout,
            error_msg="custom_install_cmd failed",
        )

    async def _create_sys_path_links(self) -> None:
        """Create symlinks in target directory for executables."""
        if self._extra_symlink_dir is None:
            return
        if not self._extra_symlink_executables:
            return

        # Build a single command with all symlinks
        links = " && ".join(
            f"ln -sf {shlex.quote(f'{self.bin_dir}/{exe}')} {shlex.quote(f'{self._extra_symlink_dir}/{exe}')}"
            for exe in self._extra_symlink_executables
        )
        await self.run(links)
