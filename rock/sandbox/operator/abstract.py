from abc import ABC, abstractmethod

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DeploymentConfig
from rock.utils.providers.redis_provider import RedisProvider


class AbstractOperator(ABC):
    _redis_provider: RedisProvider | None = None

    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        ...

    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        ...

    @abstractmethod
    async def stop(self, sandbox_id: str) -> bool:
        ...

    def set_redis_provider(self, redis_provider: RedisProvider):
        self._redis_provider = redis_provider
