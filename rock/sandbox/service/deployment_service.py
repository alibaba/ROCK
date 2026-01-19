from abc import abstractmethod
import asyncio
from rock.deployments.abstract import AbstractDeployment
import ray
from rock.deployments.ray import RayDeployment
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor

logger = init_logger(__name__)


class AbstractDeploymentService():
    @abstractmethod
    async def get_deployment(self, sandbox_id: str) -> AbstractDeployment:
        ...


class RayDeploymentService():

    async def async_ray_get_actor(self, sandbox_id: str):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor, ray.get_actor, self.deployment_manager.get_actor_name(sandbox_id), self._ray_namespace
            )
        except Exception as e:
            logger.error("ray get actor failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get actor failed, {str(e)}"
            raise Exception(error_msg)
        return result
    
    async def get_deployment(self, sandbox_id: str) -> AbstractDeployment:
        deployment: RayDeployment = RayDeployment(sandbox_id)
        try:
            sandbox_actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        except Exception as e:
            logger.error("failed to get deployment, ray get actor failed", exc_info=e)
            return None
        deployment._ray_actor = sandbox_actor
        return deployment
        
        