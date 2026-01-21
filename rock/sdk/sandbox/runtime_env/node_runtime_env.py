from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import NodeRuntimeEnvConfig
from rock.sdk.sandbox.utils import arun_with_retry, with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class NodeRuntimeEnv(RuntimeEnv):
    """Node runtime env.

    Each NodeRuntimeEnv is identified by (type, version) and is managed by Sandbox.runtime_envs.
    workdir is auto-generated as: /rock-runtime-envs/node/{version}/

    Usage:
        env = NodeRuntimeEnv(sandbox, version="20.10.0")
        await env.init()  # Installs Node runtime
        await env.run("node --version")
    """

    # Default Node version
    DEFAULT_VERSION = "22.18.0"

    runtime_env_type: str = "node"

    def __init__(
        self,
        sandbox: Sandbox,
        runtime_env_config: NodeRuntimeEnvConfig,
    ) -> None:
        super().__init__(sandbox=sandbox, runtime_env_config=runtime_env_config)

        if runtime_env_config.version not in ["default", self.DEFAULT_VERSION]:
            raise ValueError(
                f"Unsupported Node version: {runtime_env_config.version}. Only {self.DEFAULT_VERSION} is supported right now."
            )

        self.node_install_cmd = env_vars.ROCK_RTENV_NODE_V22180_INSTALL_CMD
        self.npm_registry = runtime_env_config.npm_registry

    async def _do_init(self) -> None:
        """Initialize and install Node runtime environment.

        This method:
        1. Creates workdir
        2. Installs Node runtime
        3. Configures npm registry (if specified)
        4. Validates Node exists
        """
        await self.ensure_session()

        # Step 1: ensure workdir exists
        await self._ensure_workdir()

        # Step 2: install node/npm
        await self._install_node_runtime()

        # Step 3: validate node exists
        await self._validate_node()

        # Step 4: configure npm registry if specified
        if self.npm_registry:
            await self._configure_npm_registry()

    @with_time_logging("Ensuring workdir exists")
    async def _ensure_workdir(self) -> None:
        """Create workdir for runtime environment."""
        result = await self._sandbox.arun(
            cmd=f"mkdir -p {self.workdir}",
            session=self.session,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to create workdir: {self.workdir}, exit_code: {result.exit_code}")

    @with_time_logging("Installing Node runtime")
    async def _install_node_runtime(self) -> None:
        """Install Node runtime."""
        from rock.sdk.sandbox.client import RunMode

        install_cmd = f"cd {self.workdir} && {self.node_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.install_timeout,
            error_msg="Node runtime installation failed",
        )

    @with_time_logging("Validating Node installation")
    async def _validate_node(self) -> None:
        """Validate Node executable exists."""
        return await self.run(cmd="test -x node && node --version || true")

    @with_time_logging("Configuring npm registry")
    async def _configure_npm_registry(self) -> None:
        """Configure npm registry."""
        return await self.run(cmd=f"npm config set registry {shlex.quote(self.npm_registry)}")
