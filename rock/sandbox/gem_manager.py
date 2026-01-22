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
from rock.sandbox.sandbox_manager import SandboxManager
from rock.utils.providers import RedisProvider
from rock.admin.core.ray_service import RayService


class GemManager(SandboxManager):
    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        ray_service: RayService | None = None,
        enable_runtime_auto_clear: bool = False,
    ):
        super().__init__(rock_config, redis_provider, ray_namespace, ray_service, enable_runtime_auto_clear)

    async def env_make(self, env_id: str) -> EnvMakeResponse:
        config = DockerDeploymentConfig(image=env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE)
        sandbox_start_response: SandboxStartResponse = await self.submit(config=config)

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

        make_response = await self._deployment_service.env_make(
            EnvMakeRequest(
                env_id=env_id,
                sandbox_id=sandbox_start_response.sandbox_id,
            )
        )
        return make_response
    
    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        return await self._deployment_service.env_step(request)

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        return await self._deployment_service.env_reset(request)

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        return await self._deployment_service.env_close(request)

    async def env_list(self, sandbox_id: str) -> EnvListResponse:
        return await self._deployment_service.env_list(sandbox_id)
