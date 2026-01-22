from abc import abstractmethod
import asyncio
from rock.actions.envs.request import EnvCloseRequest, EnvMakeRequest, EnvResetRequest, EnvStepRequest
from rock.actions.envs.response import EnvCloseResponse, EnvListResponse, EnvMakeResponse, EnvResetResponse, EnvStepResponse
from rock.actions.sandbox.response import CommandResponse, State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.abstract import AbstractDeployment
import ray
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Status
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.format import parse_memory_size

logger = init_logger(__name__)


class AbstractDeploymentService():
    @abstractmethod
    async def get_deployment(self, sandbox_id: str) -> AbstractDeployment:
        ...

    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict) -> SandboxInfo:
        """Get status of sandbox."""
        ...

    @abstractmethod
    async def get_status(self, *args, **kwargs) -> SandboxInfo:
        """Get status of sandbox."""
        ...

    @abstractmethod
    async def stop(self, *args, **kwargs):
        """Stop sandbox."""

    @abstractmethod
    async def get_mount(self, *args, **kwargs):
        """Get mount of sandbox."""
        ...

    @abstractmethod
    async def get_sandbox_statistics(self, *args, **kwargs):
        """Get sandbox statistics."""
        ...

    @abstractmethod
    async def commit(self, *args, **kwargs) -> CommandResponse:
        ...

    @abstractmethod
    async def env_step(self, *args, **kwargs):
        ...

    @abstractmethod
    async def env_make(self, *args, **kwargs):
        ...
    
    @abstractmethod
    async def env_reset(self, *args, **kwargs):
        ...

    @abstractmethod
    async def env_list(self, *args, **kwargs):
        ...

    @abstractmethod
    async def env_close(self, *args, **kwargs):
        ...

class RayDeploymentService():
    def __init__(self, ray_namespace: str):
        self._ray_namespace = ray_namespace

    def _get_actor_name(self, sandbox_id):
        return f"sandbox-{sandbox_id}"

    async def async_ray_get_actor(self, sandbox_id: str):
        """Async wrapper for ray.get_actor() using asyncio.to_thread for non-blocking execution."""
        try:
            actor_name = self._get_actor_name(sandbox_id)
            result = await asyncio.to_thread(ray.get_actor, actor_name, namespace=self._ray_namespace)
        except ValueError as e:
            logger.error(f"ray get actor, actor {sandbox_id} not exist", exc_info=e)
            raise e
        except Exception as e:
            logger.error("ray get actor failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get actor failed, {str(e)}"
            raise Exception(error_msg)
        return result

    async def async_ray_get(self, ray_future: ray.ObjectRef):
        """Async wrapper for ray.get() using asyncio.to_thread for non-blocking execution."""
        try:
            # Use asyncio.to_thread to run ray.get in a thread pool without managing executor
            result = await asyncio.to_thread(ray.get, ray_future, timeout=60)
        except Exception as e:
            logger.error("ray get failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get failed, {str(e)}"
            raise Exception(error_msg)
        return result

    async def submit(self, config: DockerDeploymentConfig, user_info: dict) -> SandboxInfo:
        sandbox_actor: SandboxActor = await self.creator_actor(config)
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")
        rock_authorization = user_info.get("rock_authorization", "default")
        sandbox_actor.start.remote()
        sandbox_actor.set_user_id.remote(user_id)
        sandbox_actor.set_experiment_id.remote(experiment_id)
        sandbox_actor.set_namespace.remote(namespace)
        sandbox_info: SandboxInfo = await self.async_ray_get(sandbox_actor.sandbox_info.remote())
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
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        await self.async_ray_get(actor.stop.remote())
        logger.info(f"run time stop over {sandbox_id}")
        ray.kill(actor)

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        sandbox_info: SandboxInfo = await self.async_ray_get(actor.sandbox_info.remote())
        remote_status: ServiceStatus = await self.async_ray_get(actor.get_status.remote())
        sandbox_info["phases"] = remote_status.phases
        sandbox_info["port_mapping"] = remote_status.get_port_mapping()
        alive = await self.async_ray_get(actor.is_alive.remote())
        sandbox_info["alive"] = alive.is_alive
        if alive.is_alive:
            sandbox_info["state"] = State.RUNNING
        return sandbox_info

    async def get_mount(self, sandbox_id: str):
        actor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.get_mount.remote())
        logger.info(f"get_mount: {result}")
        return result

    async def get_sandbox_statistics(self, sandbox_id: str):
        actor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.get_sandbox_statistics.remote())
        logger.info(f"get_sandbox_statistics: {result}")
        return result

    async def commit(self, sandbox_id) -> CommandResponse:
        actor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.commit.remote())
        logger.info(f"commit: {result}")
        return result

    # TODO: considering modify the result to deployment inside sandbox actor
    async def get_deployment(self, sandbox_id: str) -> AbstractDeployment:
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        status: ServiceStatus = await self.async_ray_get(actor.get_status.remote())
        logger.info(f"get_deployment: {status}")
        return status.phases["docker_run"] == Status.RUNNING
    
    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.env_step.remote(request))
        logger.info(f"env_step: {result}")
        return result
    
    async def env_make(self, request: EnvMakeRequest) -> EnvMakeResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.env_make.remote(request))
        logger.info(f"env_make: {result}")
        return result

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.env_reset.remote(request))
        logger.info(f"env_reset: {result}")
        return result

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        sandbox_id = request.sandbox_id
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.env_close.remote(request))
        logger.info(f"env_close: {result}")
        return result

    async def env_list(self, sandbox_id) -> EnvListResponse:
        actor: SandboxActor = await self.async_ray_get_actor(sandbox_id)
        result = await self.async_ray_get(actor.env_list.remote())
        logger.info(f"env_list: {result}")
        return result
