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
from rock.deployments.abstract import AbstractDeployment, DeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.sandbox.base_actor import BaseActor


class GemActor(BaseActor):
    def __init__(
        self,
        config: DeploymentConfig,
        deployment: AbstractDeployment,
    ):
        super().__init__(config, deployment)

    async def env_make(self, request: EnvMakeRequest) -> EnvMakeResponse:
        if not isinstance(self._deployment, DockerDeployment):
            return None
        return await self._deployment.runtime.env_make(request)

    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        if not isinstance(self._deployment, DockerDeployment):
            return None
        return await self._deployment.runtime.env_step(request)

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        if not isinstance(self._deployment, DockerDeployment):
            return None
        return await self._deployment.runtime.env_reset(request)

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        if not isinstance(self._deployment, DockerDeployment):
            return None
        return await self._deployment.runtime.env_close(request)

    async def env_list(self) -> EnvListResponse:
        if not isinstance(self._deployment, DockerDeployment):
            return None
        return await self._deployment.runtime.env_list()
