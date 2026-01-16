from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.node_env import NodeRuntimeEnv
from rock.sdk.sandbox.runtime_env.python_env import PythonRuntimeEnv

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class RuntimeEnvManager:
    """Manager for runtime environments.

    Supports managing multiple runtime environments of different types and versions.
    Currently supports Python and Node runtime environments.

    Usage:
        manager = RuntimeEnvManager(sandbox)
        await manager.init("python", version="3.11", pip=["langchain", "langchain-openai"])
        await manager.init("node", version="22.18.0")
        env = manager.get("python", version="3.11")
    """

    # Registry mapping runtime type to its class
    _runtime_env_registry: dict[str, type] = {
        "python": PythonRuntimeEnv,
        "node": NodeRuntimeEnv,
    }

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox
        self._runtime_envs: dict[str, Any] = {}

    @classmethod
    def register(cls, type: str, env_class: type) -> None:
        """Register a runtime environment class.

        Args:
            type: Runtime type identifier.
            env_class: Runtime environment class.
        """
        cls._runtime_env_registry[type] = env_class
        logger.info(f"Registered runtime environment type: {type}")

    async def init(self, type: str, *args, **kwargs) -> None:
        """Initialize a runtime environment.

        Args:
            type: Runtime type (e.g., "python", "node").
            *args: Additional positional arguments to pass to runtime env.
            **kwargs: Additional keyword arguments to pass to runtime env.

        Raises:
            ValueError: If type is not supported.
        """
        version = kwargs.get("version", "default")
        env_key = self._get_env_key(type, version)

        if env_key in self._runtime_envs:
            logger.info(f"Runtime environment {env_key} already initialized, skipping.")
            return

        if type not in self._runtime_env_registry:
            raise ValueError(
                f"Unsupported runtime type: {type}. " f"Supported types: {list(self._runtime_env_registry.keys())}"
            )

        env_class = self._runtime_env_registry[type]
        env = env_class(sandbox=self._sandbox, *args, **kwargs)
        await env.init()
        self._runtime_envs[env_key] = env
        logger.info(f"Initialized {type} runtime environment (version={version})")

    def get(self, type: str, version: str = "default") -> Any:
        """Get a runtime environment.

        Args:
            type: Runtime type.
            version: Runtime version.

        Returns:
            The runtime environment instance.

        Raises:
            KeyError: If the runtime environment is not initialized.
        """
        env_key = self._get_env_key(type, version)

        if env_key not in self._runtime_envs:
            raise KeyError(
                f"Runtime environment {env_key} not initialized. "
                f"Call init() first with type={type}, version={version}."
            )

        return self._runtime_envs[env_key]

    def _get_env_key(self, type: str, version: str) -> str:
        """Generate a unique key for a runtime environment."""
        return f"{type}_{version}"

    async def cleanup(self) -> None:
        """Clean up all runtime environments.

        This method is optional and can be called to explicitly clean up resources.
        """
        logger.info("Cleaning up runtime environments")
        self._runtime_envs.clear()
