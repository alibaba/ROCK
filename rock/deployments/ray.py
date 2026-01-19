import asyncio
import ray
from typing_extensions import Self

from rock import BadRequestRockError
from rock.actions.sandbox.response import CommandResponse
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor
from rock.utils.format import parse_memory_size
from rock.rocklet import __version__ as swe_version
from rock.sandbox import __version__ as gateway_version

logger = init_logger(__name__)


class RayDeployment(DockerDeployment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    async def async_ray_get(self, ray_future: ray.ObjectRef):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, lambda r: ray.get(r, timeout=60), ray_future)
        except Exception as e:
            logger.error("ray get failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get failed, {str(e)}"
            raise Exception(error_msg)
        return result
    
    async def submit(self, sandbox_id: str, user_info: dict) -> SandboxInfo:
        sandbox_actor: SandboxActor = await self.creator_actor(sandbox_id)
        self._ray_actor = sandbox_actor
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")
        sandbox_actor.start.remote()
        sandbox_actor.set_user_id.remote(user_id)
        sandbox_actor.set_experiment_id.remote(experiment_id)
        sandbox_actor.set_namespace.remote(namespace)
        sandbox_info: SandboxInfo = await self.async_ray_get(sandbox_actor.sandbox_info.remote())
        sandbox_info["user_id"] = user_id
        sandbox_info["experiment_id"] = experiment_id
        sandbox_info["namespace"] = namespace
        return sandbox_info

    async def creator_actor(self, actor_name: str):
        return await self._create_sandbox_actor(actor_name)

    async def _create_sandbox_actor(self, actor_name: str):
        actor_options = self._generate_actor_options(actor_name)
        sandbox_actor = SandboxActor.options(**actor_options).remote(self._config, self)
        return sandbox_actor

    def _generate_actor_options(self, actor_name: str) -> dict:
        actor_options = {"name": actor_name, "lifetime": "detached"}
        try:
            memory = parse_memory_size(self._config.memory)
            actor_options["num_cpus"] = self._config.cpus
            actor_options["memory"] = memory
            return actor_options
        except ValueError as e:
            logger.warning(f"Invalid memory size: {self._config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {self._config.memory}")
        
    async def stop(self, sandbox_id: str):
        actor: SandboxActor = self._ray_actor
        await self.async_ray_get(actor.stop.remote())
        logger.info(f"run time stop over {sandbox_id}")
        ray.kill(actor)

    async def get_status(self, sandbox_id: str) -> SandboxStatusResponse:
        actor: SandboxActor = self._ray_actor
        sandbox_info: SandboxInfo = await self.async_ray_get(actor.sandbox_info.remote())
        remote_status: ServiceStatus = await self.async_ray_get(actor.get_status.remote())
        alive = await self.async_ray_get(actor.is_alive.remote())
        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            status=self._service_status.phases,
            port_mapping=remote_status.get_port_mapping(),
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
            is_alive=alive.is_alive,
            image=sandbox_info.get("image"),
            swe_rex_version=swe_version,
            gateway_version=gateway_version,
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
            namespace=sandbox_info.get("namespace"),
        )
    
    async def get_mount(self, *args, **kwargs):
        actor = await self._ray_actor
        result = await self.async_ray_get(actor.get_mount.remote())
        logger.info(f"get_mount: {result}")
        return result
    
    async def get_sandbox_statistics(self, *args, **kwargs):
        actor = await self._ray_actor
        result = await self.async_ray_get(actor.get_sandbox_statistics.remote())
        logger.info(f"get_sandbox_statistics: {result}")
        return result
    
    async def commit(self, *args, **kwargs) -> CommandResponse:
        actor = await self._ray_actor
        result = await self.async_ray_get(actor.commit.remote(*args, **kwargs))
        logger.info(f"commit: {result}")
        return result
