from abc import abstractmethod

from rock.actions.sandbox.response import CommandResponse, State, SystemResourceMetrics
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
import ray
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.format import parse_memory_size

logger = init_logger(__name__)


class AbstractDeploymentService():
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

    @abstractmethod
    async def get_mount(self, sandbox_id: str):
        """Get mount of sandbox."""
        ...

    @abstractmethod
    async def get_sandbox_statistics(self, sandbox_id: str):
        """Get sandbox statistics."""
        ...

    @abstractmethod
    async def commit(self, sandbox_id: str, image_tag: str, username: str, password: str) -> CommandResponse:
        ...

    @abstractmethod
    async def collect_system_resource_metrics(self) -> SystemResourceMetrics:
        ...

class RayDeploymentService():
    def __init__(self, ray_namespace: str, ray_service: RayService):
        self._ray_namespace = ray_namespace
        self._ray_service = ray_service

    def _get_actor_name(self, sandbox_id):
        return f"sandbox-{sandbox_id}"

    async def is_alive(self, sandbox_id) -> bool:
        try:
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
        except ValueError:
            return False
        return await self._ray_service.async_ray_get(actor.is_alive.remote())

    async def submit(self, config: DockerDeploymentConfig, user_info: dict) -> SandboxInfo:
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_actor: SandboxActor = await self.creator_actor(config)
            user_id = user_info.get("user_id", "default")
            experiment_id = user_info.get("experiment_id", "default")
            namespace = user_info.get("namespace", "default")
            rock_authorization = user_info.get("rock_authorization", "default")
            sandbox_actor.start.remote()
            sandbox_actor.set_user_id.remote(user_id)
            sandbox_actor.set_experiment_id.remote(experiment_id)
            sandbox_actor.set_namespace.remote(namespace)
            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(sandbox_actor.sandbox_info.remote())
            sandbox_info["user_id"] = user_id
            sandbox_info["experiment_id"] = experiment_id
            sandbox_info["namespace"] = namespace
            sandbox_info["state"] = State.PENDING
            sandbox_info["rock_authorization"] = rock_authorization
            return sandbox_info

    async def creator_actor(self, config: DockerDeploymentConfig):
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

    async def stop(self, sandbox_id: str):
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            await self._ray_service.async_ray_get(actor.stop.remote())
            logger.info(f"run time stop over {sandbox_id}")
            ray.kill(actor)

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
            return sandbox_info

    async def get_mount(self, sandbox_id: str):
        with self._ray_service.get_ray_rwlock().read_lock():
            actor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            result = await self._ray_service.async_ray_get(actor.get_mount.remote())
            logger.info(f"get_mount: {result}")
            return result

    async def get_sandbox_statistics(self, sandbox_id: str):
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            result = await self._ray_service.async_ray_get(actor.get_sandbox_statistics.remote())
            logger.info(f"get_sandbox_statistics: {result}")
            return result

    async def commit(self, sandbox_id) -> CommandResponse:
        with self._ray_service.get_ray_rwlock().read_lock():
            actor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            result = await self._ray_service.async_ray_get(actor.commit.remote())
            logger.info(f"commit: {result}")
            return result

    async def collect_system_resource_metrics(self) -> SystemResourceMetrics:
        """Collect system resource metrics"""
        cluster_resources = ray.cluster_resources()
        available_resources = ray.available_resources()
        total_cpu = cluster_resources.get("CPU", 0)
        total_mem = cluster_resources.get("memory", 0) / 1024**3
        available_cpu = available_resources.get("CPU", 0)
        available_mem = available_resources.get("memory", 0) / 1024**3
        gpu_count = cluster_resources.get("GPU", 0)
        available_gpu = available_resources.get("GPU", 0)

        return SystemResourceMetrics(
            total_cpu=total_cpu,
            total_memory=total_mem,
            available_cpu=available_cpu,
            available_memory=available_mem,
            gpu_count=int(gpu_count),
            available_gpu=int(available_gpu),
        )
