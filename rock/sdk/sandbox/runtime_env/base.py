from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import RunModeType, Sandbox


class RuntimeEnv(ABC):
    """Runtime environment (e.g., Python/Node).

    Key points:
    - Runtime env maintains its own bash session.
    - Runtime env is responsible for creating that session if missing.
    - Runtime env provides wrap/run helpers to execute commands under that runtime context.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        workdir: str,
        session: str = "agent-runtime-env-session",
        version: str | None = None,
        add_to_sys_path: bool = False,
        init_timeout: int = 300,
        session_envs: dict[str, str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self.session = session
        self.session_envs = session_envs or {}
        self._prepared: bool = False
        self._session_ready: bool = False
        self.workdir = workdir
        self.version = version
        self.add_to_sys_path = add_to_sys_path
        self.init_timeout = init_timeout

    @property
    def prepared(self) -> bool:
        return self._prepared

    async def ensure_session(self) -> None:
        """Ensure runtime env session exists. Safe to call multiple times."""
        if self._session_ready:
            return

        # Try to create; if already exists, sandbox may raise—treat as ok.
        try:
            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.session,
                    env_enable=True,
                    env=self.session_envs,
                )
            )
        except Exception:
            pass

        self._session_ready = True

    @abstractmethod
    async def init(self) -> None:
        """Initialize the runtime in the sandbox (install/unpack, validate)."""
        raise NotImplementedError

    @abstractmethod
    def wrap(self, cmd: str) -> str:
        """Wrap a shell command so it runs under this runtime (e.g., inject PATH)."""
        raise NotImplementedError

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
        return await arun_with_retry(
            sandbox=self._sandbox,
            cmd=wrapped,
            session=self.session,
            mode=mode,
            wait_timeout=wait_timeout,
            error_msg=error_msg,
        )
