from abc import ABC, abstractmethod

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import alive_sandbox_key
from rock.common.constants import StopReason
from rock.config import RuntimeConfig
from rock.deployments.config import DeploymentConfig
from rock.utils.providers.nacos_provider import NacosConfigProvider
from rock.utils.providers.redis_provider import RedisProvider


class AbstractOperator(ABC):
    _redis_provider: RedisProvider | None = None
    _nacos_provider: NacosConfigProvider | None = None
    _runtime_config: RuntimeConfig | None = None

    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        ...

    @abstractmethod
    async def restart(self, config: DeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        """Restart an existing stopped container using docker start.

        The actor for this sandbox has already been killed by stop().
        Implementations must create a new actor and invoke docker start
        on the existing (stopped) container — not docker run.
        """
        ...

    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        ...

    @abstractmethod
    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        ...

    @abstractmethod
    async def delete(self, config: DeploymentConfig, host_ip: str | None = None) -> bool:
        ...

    def set_redis_provider(self, redis_provider: RedisProvider):
        self._redis_provider = redis_provider

    def set_nacos_provider(self, nacos_provider: NacosConfigProvider):
        self._nacos_provider = nacos_provider

    async def get_sandbox_info_from_redis(self, sandbox_id: str) -> dict | None:
        if not self._redis_provider:
            raise RuntimeError("Redis provider is not configured")
        sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if sandbox_status and len(sandbox_status) > 0:
            return sandbox_status[0]
        return None
