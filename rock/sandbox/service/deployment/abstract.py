from abc import ABC, abstractmethod

from rock.actions.sandbox.response import CommandResponse, SystemResourceMetrics
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DeploymentConfig
from rock.logger import init_logger

logger = init_logger(__name__)

class AbstractDeploymentService(ABC):
    """Abstract base class for deployment services implementing IDeploymentService."""
    
    @abstractmethod
    async def is_alive(self, sandbox_id: str) -> bool:
        ...

    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict) -> SandboxInfo:
        """Get status of sandbox."""
        ...

    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get status of sandbox."""
        ...

    @abstractmethod
    async def stop(self, sandbox_id: str):
        """Stop sandbox."""

    async def get_mount(self, sandbox_id: str):
        """Get mount of sandbox."""
        raise NotImplementedError

    async def get_sandbox_statistics(self, sandbox_id: str):
        """Get sandbox statistics."""
        raise NotImplementedError

    async def commit(self, sandbox_id: str, image_tag: str, username: str, password: str) -> CommandResponse:
        """Commit sandbox to image."""
        raise NotImplementedError

    async def collect_system_resource_metrics(self) -> SystemResourceMetrics:
        """Collect system resource metrics."""
        raise NotImplementedError