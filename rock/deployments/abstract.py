import asyncio
from abc import ABC, abstractmethod

from rock.actions import AbstractSandbox, IsAliveResponse
from rock.actions.sandbox.response import CommandResponse
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.deployments.hooks.abstract import DeploymentHook
from rock.logger import init_logger

__all__ = ["AbstractDeployment"]
logger = init_logger(__name__)


class AbstractDeployment(ABC):
    @abstractmethod
    def add_hook(self, hook: DeploymentHook):
        ...

    @abstractmethod
    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive. The return value can be
        tested with bool().

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """

    @abstractmethod
    async def start(self, *args, **kwargs):
        """Starts the runtime."""

    @abstractmethod
    async def stop(self, *args, **kwargs):
        """Stops the runtime."""

    # @abstractmethod
    async def creator_actor(self, actor_name: str):
        """create actor."""
        return None
    
    async def submit(self, sandbox_id: str) -> SandboxStartResponse:
        """Get status of sandbox."""

    async def get_status(self, *args, **kwargs) -> SandboxStatusResponse:
        """Get status of sandbox."""

    async def get_mount(self, *args, **kwargs):
        """Get mount of sandbox."""

    async def get_sandbox_statistics(self, *args, **kwargs):
        """Get sandbox statistics."""

    async def commit(self, *args, **kwargs) -> CommandResponse:
        """Commit sandbox."""

    @property
    @abstractmethod
    def runtime(self) -> AbstractSandbox:
        """Returns the runtime if running.

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """

    def __del__(self):
        """Stops the runtime when the object is deleted."""
        # Need to be check whether we are in an async event loop or not
        # https://stackoverflow.com/questions/54770360/
        msg = "Ensuring deployment is stopped because object is deleted"
        try:
            logger.debug(msg)
        except Exception:
            print(msg)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.stop())
            else:
                loop.run_until_complete(self.stop())
        except Exception:
            pass
