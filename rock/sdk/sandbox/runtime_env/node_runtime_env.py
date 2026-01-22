from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from typing_extensions import override

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import RuntimeEnvConfig

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class NodeRuntimeEnvConfig(RuntimeEnvConfig):
    """Configuration for Node.js runtime environment.

    Example:
        runtime_env_config=NodeRuntimeEnvConfig(
            version="default",  # defaults to 22.18.0
            npm_registry="https://registry.npmmirror.com",
        )
    """

    type: Literal["node"] = Field(default="node")
    """Runtime type discriminator. Must be 'node'."""

    version: Literal["22.18.0", "default"] = Field(default="default")
    """Node.js version. Use "default" for 22.18.0."""

    npm_registry: str | None = Field(default=None)
    """NPM registry URL. If set, will run 'npm config set registry <url>' during init."""


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
        if runtime_env_config.version not in ["default", self.DEFAULT_VERSION]:
            raise ValueError(
                f"Unsupported Node version: {runtime_env_config.version}. Only {self.DEFAULT_VERSION} is supported right now."
            )

        super().__init__(sandbox=sandbox, runtime_env_config=runtime_env_config)

        self._npm_registry = runtime_env_config.npm_registry

    def _get_install_cmd(self) -> str:
        return env_vars.ROCK_RTENV_NODE_V22180_INSTALL_CMD

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
