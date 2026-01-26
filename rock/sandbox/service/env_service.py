from abc import ABC, abstractmethod

from rock.actions.envs.request import EnvCloseRequest, EnvMakeRequest, EnvResetRequest, EnvStepRequest
from rock.actions.envs.response import EnvCloseResponse, EnvListResponse, EnvMakeResponse, EnvResetResponse, EnvStepResponse
from rock.admin.core.ray_service import RayService
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor

logger = init_logger(__name__)


class AbstractEnvService(ABC):
    @abstractmethod
    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        ...

    @abstractmethod
    async def env_make(self, request: EnvMakeRequest) -> EnvMakeResponse:
        ...
    
    @abstractmethod
    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        ...

    @abstractmethod
    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        ...
    
    @abstractmethod
    async def env_list(self, sandbox_id) -> EnvListResponse:
        ...


class RayEnvService(AbstractEnvService):
    def __init__(self, ray_namespace: str, ray_service: RayService):
        self._ray_namespace = ray_namespace
        self._ray_service = ray_service

    def _get_actor_name(self, sandbox_id):
        return f"sandbox-{sandbox_id}"

    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        result = await self._ray_service.async_ray_get(actor.env_step.remote(request))
        logger.info(f"env_step: {result}")
        return result

    async def env_make(self, request: EnvMakeRequest) -> EnvMakeResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        result = await self._ray_service.async_ray_get(actor.env_make.remote(request))
        logger.info(f"env_make: {result}")
        return result

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        result = await self._ray_service.async_ray_get(actor.env_reset.remote(request))
        logger.info(f"env_reset: {result}")
        return result

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        result = await self._ray_service.async_ray_get(actor.env_close.remote(request))
        logger.info(f"env_close: {result}")
        return result

    async def env_list(self, sandbox_id) -> EnvListResponse:
        actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        result = await self._ray_service.async_ray_get(actor.env_list.remote())
        logger.info(f"env_list: {result}")
        return result