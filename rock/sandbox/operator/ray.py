import json

import ray

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.admin.core.redis_key import alive_sandbox_key
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.format import parse_memory_size

logger = init_logger(__name__)


class RayOperator(AbstractOperator):
    def __init__(self, ray_service: RayService):
        self._ray_service = ray_service

    def _get_actor_name(self, sandbox_id: str) -> str:
        return f"sandbox-{sandbox_id}"

    async def create_actor(self, config: DockerDeploymentConfig):
        actor_options = self._generate_actor_options(config)
        deployment: DockerDeployment = config.get_deployment()
        sandbox_actor = SandboxActor.options(**actor_options).remote(config, deployment)
        return sandbox_actor

    def _generate_actor_options(self, config: DockerDeploymentConfig) -> dict:
        actor_name = self._get_actor_name(config.container_name)
        actor_options = {"name": actor_name, "lifetime": "detached"}
        try:
            memory = parse_memory_size(config.memory)
            actor_options["num_cpus"] = config.cpus
            actor_options["memory"] = memory
            return actor_options
        except ValueError as e:
            logger.warning(f"Invalid memory size: {config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {config.memory}")

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_id = config.container_name
            logger.info(f"[{sandbox_id}] start_async params:{json.dumps(config.model_dump(), indent=2)}")
            sandbox_actor: SandboxActor = await self.create_actor(config)
            sandbox_actor.start.remote()
            user_id = user_info.get("user_id", "default")
            experiment_id = user_info.get("experiment_id", "default")
            namespace = user_info.get("namespace", "default")
            rock_authorization = user_info.get("rock_authorization", "default")
            sandbox_actor.set_user_id.remote(user_id)
            sandbox_actor.set_experiment_id.remote(experiment_id)
            sandbox_actor.set_namespace.remote(namespace)
            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(sandbox_actor.sandbox_info.remote())
            sandbox_info["user_id"] = user_id
            sandbox_info["experiment_id"] = experiment_id
            sandbox_info["namespace"] = namespace
            sandbox_info["state"] = State.PENDING
            sandbox_info["rock_authorization"] = rock_authorization
            logger.info(f"sandbox {sandbox_id} is submitted")
            return sandbox_info

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(actor.sandbox_info.remote())
            remote_status: ServiceStatus = await self._ray_service.async_ray_get(actor.get_status.remote())
            sandbox_info["phases"] = remote_status.phases
            sandbox_info["port_mapping"] = remote_status.get_port_mapping()
            alive = await self._ray_service.async_ray_get(actor.is_alive.remote())
            if alive.is_alive:
                sandbox_info["state"] = State.RUNNING
            if not self._redis_provider:
                return sandbox_info
            redis_info = await self.get_sandbox_info_from_redis(sandbox_id)
            if redis_info:
                redis_info.update(sandbox_info)
                redis_info["phases"] = {name: phase.to_dict() for name, phase in remote_status.phases.items()}
                return redis_info
            else:
                return sandbox_info
            # return sandbox_info

    async def get_sandbox_info_from_redis(self, sandbox_id: str) -> SandboxInfo:
        sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if sandbox_status and len(sandbox_status) > 0:
            sandbox_info = sandbox_status[0]
            return sandbox_info
        return None

    async def stop(self, sandbox_id: str) -> bool:
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            await self._ray_service.async_ray_get(actor.stop.remote())
            logger.info(f"run time stop over {sandbox_id}")
            ray.kill(actor)
            return True
