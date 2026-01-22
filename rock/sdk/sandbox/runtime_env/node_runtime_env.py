from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from typing_extensions import override

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import NodeRuntimeEnvConfig

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

        self._install_cmd = env_vars.ROCK_RTENV_NODE_V22180_INSTALL_CMD
        self._npm_registry = runtime_env_config.npm_registry

    @property
    def install_cmd(self) -> str:
        return self._install_cmd

    @override
    async def _post_init(self) -> None:
        """Additional initialization after runtime installation.

        This method:
        1. Validates Node exists
        2. Configures npm registry (if specified)
        """
        # Step 1: validate node exists
        await self._validate_node()

        # Step 2: configure npm registry if specified
        if self._npm_registry:
            await self._configure_npm_registry()

    async def _validate_node(self) -> None:
        """Validate Node executable exists."""
        return await self.run(cmd="test -x node")

    async def _configure_npm_registry(self) -> None:
        """Configure npm registry."""
        return await self.run(cmd=f"npm config set registry {shlex.quote(self._npm_registry)}")
