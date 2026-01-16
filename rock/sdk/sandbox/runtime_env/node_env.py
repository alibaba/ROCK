from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class NodeRuntimeEnv(RuntimeEnv):
    """Node runtime env.

    This is a minimal runtime env:
    - init(): ensures workdir exists, then runs node_install_cmd in that workdir
    - wrap(): currently just runs via bash -c (no fixed layout enforced)
      (If later you standardize node to a fixed folder under workdir, you can inject PATH here.)
    """

    def __init__(
        self,
        sandbox: Sandbox,
        workdir: str,
        session: str = "node-runtime-env-session",
        version: str | None = None,
        add_to_sys_path: bool = True,
        init_timeout: int = 300,
        session_envs: dict[str, str] | None = None,
    ) -> None:
        if not add_to_sys_path:
            raise ValueError("Node runtime env only supports add_to_sys_path=True")

        super().__init__(
            sandbox=sandbox,
            workdir=workdir,
            session=session,
            version=version,
            add_to_sys_path=add_to_sys_path,
            init_timeout=init_timeout,
            session_envs=session_envs,
        )
        self.node_install_cmd = env_vars.ROCK_AGENT_NPM_INSTALL_CMD

    def wrap(self, cmd: str) -> str:
        return f"bash -c {shlex.quote(cmd)}"

    async def init(self) -> None:
        """Initialize the Node runtime environment."""
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Initializing Node runtime env (workdir={self.workdir})")

        # 1) ensure workdir exists
        await self._sandbox.arun(
            cmd=f"mkdir -p {shlex.quote(self.workdir)}",
            session=self.session,
        )

        from rock.sdk.sandbox.client import RunMode

        # 2) install node/npm (script may install globally)
        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.node_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.init_timeout,
            error_msg="Node runtime installation failed",
        )

        # 3) lightweight validation (best-effort)
        # Some images may not have node in PATH immediately; treat failure as warning.
        res = await self._sandbox.arun(
            cmd="command -v node >/dev/null 2>&1 && node --version || true",
            session=self.session,
        )
        logger.debug(f"[{sandbox_id}] Node validation output: {res.output[:200]}")

        if res.exit_code != 0:
            raise RuntimeError("Node runtime validation failed")

        self._prepared = True
        logger.info(f"[{sandbox_id}] Node runtime env initialized")
