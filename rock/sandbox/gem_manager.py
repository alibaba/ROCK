import asyncio

from rock import env_vars
from rock.actions import (
    EnvCloseRequest,
    EnvCloseResponse,
    EnvListResponse,
    EnvMakeRequest,
    EnvMakeResponse,
    EnvResetRequest,
    EnvResetResponse,
    EnvStepRequest,
    EnvStepResponse,
)
from rock.admin.core.ray_service import RayService
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sandbox.sandbox_manager import SandboxManager
from rock.utils.providers import RedisProvider


class GemManager(SandboxManager):
    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        ray_service: RayService | None = None,
        enable_runtime_auto_clear: bool = False,
        operator=None,
    ):
        super().__init__(rock_config, redis_provider, ray_namespace, ray_service, enable_runtime_auto_clear, operator)

    async def env_make(self, env_id: str) -> EnvMakeResponse:
        config = DockerDeploymentConfig(image=env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE)
        sandbox_start_response: SandboxStartResponse = await self.start_async(config=config)

        async def wait_until_alive(sandbox_id: str, interval: float = 1.0):
            """Internal polling method"""
            while True:
                await asyncio.sleep(interval)
                status: SandboxStatusResponse = await self.get_status(sandbox_id)
                if status.is_alive:
                    return status

        try:
            await asyncio.wait_for(
                wait_until_alive(sandbox_start_response.sandbox_id),
                timeout=300.0,  # 5 minute timeout
            )
        except asyncio.TimeoutError:
            raise Exception("Sandbox startup timeout after 300s")

        actor_name = self.deployment_manager.get_actor_name(sandbox_start_response.sandbox_id)
        sandbox_actor: SandboxActor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_start_response.sandbox_id} not found to stop")
        response = await self._ray_service.async_ray_get(
            sandbox_actor.env_make.remote(
                EnvMakeRequest(
                    env_id=env_id,
                    sandbox_id=sandbox_start_response.sandbox_id,
                )
            )
        )
        return response

    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        sandbox_id = request.sandbox_id
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor: SandboxActor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to stop")
        return await self._ray_service.async_ray_get(sandbox_actor.env_step.remote(request))

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        sandbox_id = request.sandbox_id
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor: SandboxActor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to stop")
        return await self._ray_service.async_ray_get(sandbox_actor.env_reset.remote(request))

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        sandbox_id = request.sandbox_id
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor: SandboxActor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to stop")
        response = await self._ray_service.async_ray_get(sandbox_actor.env_close.remote(request))
        await self.stop(sandbox_id=sandbox_id)
        return response

    async def env_list(self, sandbox_id: str) -> EnvListResponse:
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to stop")
        return await self._ray_service.async_ray_get(sandbox_actor.env_list.remote())
