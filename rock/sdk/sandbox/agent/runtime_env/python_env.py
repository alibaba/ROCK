from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.agent.runtime_env.base import AgentRuntimeEnv
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class PythonAgentRuntimeEnv(AgentRuntimeEnv):
    """Python runtime env.

    Contract:
    - workdir/python is the runtime root directory (fixed).
    - workdir/python/bin contains python/pip and any console_scripts (e.g. sweagent).
    """

    def __init__(
        self,
        sandbox: Sandbox,
        workdir: str,
        session: str = "python-runtime-env-session",
        install_cmd: str | None = None,
        prepare_timeout: int = 300,
        session_envs: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            sandbox=sandbox,
            workdir=workdir,
            session=session,
            install_cmd=install_cmd,
            prepare_timeout=prepare_timeout,
            session_envs=session_envs,
        )
        self.python_install_cmd = install_cmd or env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD

    @property
    def bin_dir(self) -> str:
        return f"{self.workdir}/python/bin"

    def wrap(self, cmd: str) -> str:
        wrapped = f"export PATH={shlex.quote(self.bin_dir)}:$PATH && {cmd}"
        return f"bash -c {shlex.quote(wrapped)}"

    async def prepare(self) -> None:
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Preparing Python runtime env in {self.workdir}")

        # 1) ensure workdir exists
        await self._sandbox.arun(
            cmd=f"mkdir -p {shlex.quote(self.workdir)}",
            session=self.session,
        )

        from rock.sdk.sandbox.client import RunMode

        # 2) run install cmd in workdir (must create ./python)
        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.python_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.prepare_timeout,
            error_msg="Python runtime installation failed",
        )

        # 3) validate python exists
        check_cmd = f"test -x {shlex.quote(self.bin_dir)}/python"
        result = await self._sandbox.arun(
            cmd=check_cmd,
            session=self.session,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                "PythonAgentRuntimeEnv.prepare() failed: "
                f"{self.bin_dir}/python not found or not executable. "
                "Ensure python_install_cmd installs into ./python under workdir."
            )

        self._prepared = True
        logger.info(f"[{sandbox_id}] Python runtime env prepared")
