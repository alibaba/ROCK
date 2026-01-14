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


class NodeAgentRuntimeEnv(AgentRuntimeEnv):
    """Node runtime env.

    This is a minimal runtime env:
    - prepare(): ensures workdir exists, then runs node_install_cmd in that workdir
    - wrap(): currently just runs via bash -c (no fixed layout enforced)
      (If later you standardize node to a fixed folder under workdir, you can inject PATH here.)
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        workdir: str,
        session: str = "node-runtime-env-session",
        node_install_cmd: str | None = None,
        prepare_timeout: int = 300,
        session_envs: dict[str, str] | None = None,
    ) -> None:
        super().__init__(sandbox=sandbox, session=session, session_envs=session_envs)
        self.workdir = workdir
        self.node_install_cmd = node_install_cmd or env_vars.ROCK_AGENT_NPM_INSTALL_CMD
        self.prepare_timeout = prepare_timeout

    def wrap(self, cmd: str) -> str:
        return f"bash -c {shlex.quote(cmd)}"

    async def prepare(self) -> None:
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Preparing Node runtime env (workdir={self.workdir})")

        # 1) ensure workdir exists
        await self._sandbox.arun(
            cmd=f"mkdir -p {shlex.quote(self.workdir)}",
            session=self.session,
        )

        # 2) install node/npm (script may install globally)
        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.node_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode="nohup",
            wait_timeout=self.prepare_timeout,
            error_msg="Node runtime installation failed",
        )

        # 3) lightweight validation (best-effort)
        # Some images may not have node in PATH immediately; treat failure as warning.
        res = await self._sandbox.arun(
            cmd="bash -c 'command -v node >/dev/null 2>&1 && node --version || true'",
            session=self.session,
        )
        logger.debug(f"[{sandbox_id}] Node validation output: {res.output[:200]}")

        self._prepared = True
        logger.info(f"[{sandbox_id}] Node runtime env prepared")
