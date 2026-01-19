from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import NodeRuntimeEnvConfig
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class NodeRuntimeEnv(RuntimeEnv):
    """Node runtime env.

    Each NodeRuntimeEnv is identified by (type, version) and is managed by Sandbox.rt_envs.
    workdir is auto-generated as: /rock_rt_envs/node/{version}/

    Usage:
        env = NodeRuntimeEnv(sandbox, version="20.10.0")
        await env.init()  # Installs Node runtime
        await env.run("node --version")
    """

    # Default Node version
    DEFAULT_VERSION = "22.18.0"

    rt_env_type: str = "node"

    def __init__(
        self,
        sandbox: Sandbox,
        rt_env_config: NodeRuntimeEnvConfig,
    ) -> None:
        super().__init__(sandbox=sandbox, rt_env_config=rt_env_config)

        if rt_env_config.version not in ["default", self.DEFAULT_VERSION]:
            raise ValueError(
                f"Unsupported Node version: {rt_env_config.version}. Only {self.DEFAULT_VERSION} is supported right now."
            )

        self.node_install_cmd = env_vars.ROCK_RTENV_NODE_INSTALL_CMD
        self.npm_registry = rt_env_config.npm_registry

    async def _do_init(self) -> None:
        """Initialize and install Node runtime environment.

        This method:
        1. Creates workdir
        2. Installs Node runtime
        3. Configures npm registry (if specified)
        4. Validates Node exists
        5. Adds to sys path (if specified)
        """
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Initializing Node runtime in {self.workdir}")

        from rock.sdk.sandbox.client import RunMode

        # 1) ensure workdir exists
        await self._sandbox.arun(
            cmd=f"mkdir -p {self.workdir}",
            session=self.session,
        )

        # 2) install node/npm
        install_cmd = f"cd {self.workdir} && {self.node_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=600,
            error_msg="Node runtime installation failed",
        )
        logger.info(f"[{sandbox_id}] Node runtime installed")

        # 3) configure npm registry if specified
        if self.npm_registry:
            logger.info(f"[{sandbox_id}] Configuring npm registry: {self.npm_registry}")
            await self.run(cmd=f"npm config set registry {shlex.quote(self.npm_registry)}")

        # 4) validate node exists
        res = await self.run(cmd="test -x node && node --version || true")
        logger.debug(f"[{sandbox_id}] Node validation output: {res.output[:200]}")

        if res.exit_code != 0:
            raise RuntimeError("Node runtime validation failed")

        # 5) add to sys path if specified
        if self.add_to_sys_path:
            await self._add_to_sys_path(executables=["node", "npm", "npx", "corepack"])

        logger.info(f"[{sandbox_id}] Node runtime env initialized")
