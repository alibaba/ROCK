from typing import Any

from typing_extensions import Self

from rock.actions import IsAliveResponse
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import LocalDeploymentConfig
from rock.deployments.hooks.abstract import CombinedDeploymentHook, DeploymentHook
from rock.logger import init_logger
from rock.rocklet.exceptions import DeploymentNotStartedError
from rock.rocklet.rocklet import Rocklet

logger = init_logger(__name__)

__all__ = ["LocalDeployment", "LocalDeploymentConfig"]


class LocalDeployment(AbstractDeployment):
    def __init__(
        self,
        **kwargs: Any,
    ):
        """The most boring of the deployment classes.
        This class does nothing but wrap around `Runtime` so you can switch out
        your deployment method.

        Args:
            **kwargs: Keyword arguments (see `LocalDeploymentConfig` for details).
        """
        self._runtime = None
        self._config = LocalDeploymentConfig(**kwargs)
        self._hooks = CombinedDeploymentHook()

    def add_hook(self, hook: DeploymentHook):
        self._hooks.add_hook(hook)

    @classmethod
    def from_config(cls, config: LocalDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive. The return value can be
        tested with bool().

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            return IsAliveResponse(is_alive=False, message="Runtime is None.")
        return await self._runtime.is_alive(timeout=timeout)

    async def start(self):
        """Starts the runtime."""
        self._runtime = Rocklet.create()

    async def stop(self):
        """Stops the runtime."""
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    @property
    def runtime(self) -> Rocklet:
        """Returns the runtime if running.

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            raise DeploymentNotStartedError()
        return self._runtime
