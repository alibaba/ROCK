from __future__ import annotations

import shlex
import uuid
from abc import ABC
from typing import TYPE_CHECKING, NewType

from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import RunModeType, Sandbox
    from rock.sdk.sandbox.runtime_env.config import RuntimeEnvConfig

logger = init_logger(__name__)

RuntimeEnvId = NewType("RuntimeEnvId", str)


class RuntimeEnv(ABC):
    """Runtime environment (e.g., Python/Node).

    Each RuntimeEnv is identified by (type, version) tuple and is managed by Sandbox.rt_envs.
    workdir is auto-generated as: /rock-rt-envs/{type}/{version}/
    session is auto-generated as: rt-env-{type}-{version}

    Usage:
        # Factory method to create RuntimeEnv from config
        env = RuntimeEnv.from_config(sandbox, config.rt_env_config)
        await env.init()
        await env.run("python --version")
    """

    # Registry for subclasses (auto-registered by __init_subclass__)
    _REGISTRY: dict[str, type[RuntimeEnv]] = {}

    # Runtime type discriminator
    rt_env_type: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Register subclass based on its rt_env_type property
        # The subclass must define rt_env_type as a class attribute
        if hasattr(cls, "rt_env_type") and isinstance(cls.rt_env_type, str):
            cls._REGISTRY[cls.rt_env_type] = cls

    @classmethod
    def from_config(cls, sandbox: Sandbox, rt_env_config: RuntimeEnvConfig) -> RuntimeEnv:
        """Factory method to create RuntimeEnv from config.

        Args:
            sandbox: Sandbox instance
            rt_env_config: Runtime environment configuration

        Returns:
            RuntimeEnv instance of the appropriate type, automatically registered to sandbox.runtime_envs
        """
        rt_type = rt_env_config.rt_env_type
        rt_class = cls._REGISTRY.get(rt_type)
        if rt_class is None:
            raise ValueError(f"Unsupported runtime type: {rt_type}")
        rt_env = rt_class(sandbox=sandbox, rt_env_config=rt_env_config)
        # Auto-register to sandbox.runtime_envs
        sandbox.runtime_envs[rt_env.rt_env_id] = rt_env
        return rt_env

    def __init__(
        self,
        sandbox: Sandbox,
        rt_env_config: RuntimeEnvConfig,
    ) -> None:
        self._sandbox = sandbox

        # Extract values from config
        version = rt_env_config.version
        add_to_sys_path = rt_env_config.add_to_sys_path
        session_envs = rt_env_config.session_envs

        self.version = version
        self.add_to_sys_path = add_to_sys_path
        self.session_envs = session_envs or {}
        self.install_timeout = rt_env_config.install_timeout

        rt_type = rt_env_config.rt_env_type
        version_str = version or "default"

        # Unique ID for this runtime env instance
        self.rt_env_id = RuntimeEnvId(str(uuid.uuid4())[:8])

        self.workdir = f"/rock-rt-envs/{rt_type}/{version_str}/{self.rt_env_id}/"
        self.session = f"rt-env-{rt_type}-{version_str}-{self.rt_env_id}"

        # State flag
        self._initialized: bool = False
        self._session_ready: bool = False

    @property
    def bin_dir(self) -> str:
        return f"{self.workdir}/runtime-env/bin"

    @property
    def initialized(self) -> bool:
        """Whether the runtime has been initialized."""
        return self._initialized

    async def ensure_session(self) -> None:
        """Ensure runtime env session exists. Safe to call multiple times."""
        if self._session_ready:
            return

        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self.session,
                env_enable=True,
                env=self.session_envs,
            )
        )
        self._session_ready = True

    async def init(self) -> None:
        """Initialize the runtime environment.

        This method performs installation and validation.
        It is idempotent: calling multiple times only initializes once.
        Subclasses should override _do_init() to perform actual installation.
        """
        if self._initialized:
            return

        await self._do_init()
        self._initialized = True

    async def _do_init(self) -> None:
        """Internal method for initialization. Override in subclasses."""
        raise NotImplementedError

    def wrap(self, cmd: str) -> str:
        wrapped = f"export PATH={shlex.quote(self.bin_dir)}:$PATH && {cmd}"
        return f"bash -c {shlex.quote(wrapped)}"

    async def run(
        self,
        cmd: str,
        mode: RunModeType | None = None,
        wait_timeout: int = 600,
        error_msg: str = "runtime env command failed",
    ):
        """Run a command under this runtime via arun_with_retry."""
        # Import here to avoid circular import
        from rock.sdk.sandbox.client import RunMode

        if mode is None:
            mode = RunMode.NOHUP

        await self.ensure_session()
        wrapped = self.wrap(cmd)

        logger.debug(f"[{self._sandbox.sandbox_id}] RuntimeEnv run cmd: {wrapped}")

        return await arun_with_retry(
            sandbox=self._sandbox,
            cmd=wrapped,
            session=self.session,
            mode=mode,
            wait_timeout=wait_timeout,
            error_msg=error_msg,
        )

    async def _add_to_sys_path(self, executables: list[str]) -> None:
        """Link runtime bin directory to /usr/local/bin for system-wide access.

        Args:
            executables: List of executable names to symlink.
        """
        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Adding runtime bin to system path")

        for exe in executables:
            src = f"{self.bin_dir}/{exe}"
            dst = f"/usr/local/bin/{exe}"
            cmd = f"ln -sf {shlex.quote(src)} {shlex.quote(dst)}"
            result = await self._sandbox.arun(
                cmd=cmd,
                session=self.session,
            )
            if result.exit_code != 0:
                raise RuntimeError(f"Failed to create symlink for {exe}: {result.output}")

        logger.info(f"[{sandbox_id}] Runtime bin added to system path")
