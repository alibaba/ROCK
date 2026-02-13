import json

import ray

from rock import env_vars
from rock.actions.sandbox.response import IsAliveResponse, State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.admin.core.redis_key import alive_sandbox_key
from rock.common.constants import GET_STATUS_SWITCH
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import PersistedServiceStatus, ServiceStatus
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils import EAGLE_EYE_TRACE_ID, trace_id_ctx_var
from rock.utils.format import parse_memory_size
from rock.utils.http import HttpUtils
from rock.utils.service import build_sandbox_from_redis

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
        if self.use_rocklet():
            sandbox_info: SandboxInfo = await build_sandbox_from_redis(self._redis_provider, sandbox_id)
            host_ip = sandbox_info.get("host_ip")
            remote_status = await self.get_remote_status(sandbox_id, host_ip)
            is_alive = await self._check_alive_status(sandbox_id, host_ip, remote_status)
            # TODO: sink update state according to is_alive logic into SandboxInfo
            if is_alive:
                sandbox_info["state"] = State.RUNNING
            sandbox_info.update(remote_status.to_dict())
            return sandbox_info
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(actor.sandbox_info.remote())
            remote_status: ServiceStatus = await self._ray_service.async_ray_get(actor.get_status.remote())
            sandbox_info["phases"] = remote_status.phases
            sandbox_info["port_mapping"] = remote_status.get_port_mapping()
            alive = await self._ray_service.async_ray_get(actor.is_alive.remote())
            # TODO: sink update state according to is_alive logic into SandboxInfo
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

    async def _check_alive_status(self, sandbox_id: str, host_ip: str, remote_status: ServiceStatus) -> bool:
        """Check if sandbox is alive"""
        try:
            alive_resp = await HttpUtils.get(
                url=f"http://{host_ip}:{remote_status.get_mapped_port(Port.PROXY)}/is_alive",
                headers={
                    "sandbox_id": sandbox_id,
                    EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get(),
                },
            )
            return IsAliveResponse(**alive_resp).is_alive
        except Exception:
            return False

    async def get_remote_status(self, sandbox_id: str, host_ip: str) -> ServiceStatus:
        service_status_path = PersistedServiceStatus.gen_service_status_path(sandbox_id)
        worker_rocklet_port = env_vars.ROCK_WORKER_ROCKLET_PORT if env_vars.ROCK_WORKER_ROCKLET_PORT else Port.PROXY
        execute_url = f"http://{host_ip}:{worker_rocklet_port}/execute"
        read_file_url = f"http://{host_ip}:{worker_rocklet_port}/read_file"
        headers = {"sandbox_id": sandbox_id, EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get()}
        find_file_rsp = await HttpUtils.post(
            url=execute_url,
            headers=headers,
            data={"command": ["ls", service_status_path]},
            read_timeout=60,
        )

        # When the file does not exist, exit_code = 2
        if find_file_rsp.get("exit_code") and find_file_rsp.get("exit_code") == 2:
            return ServiceStatus()

        response: dict = await HttpUtils.post(
            url=read_file_url,
            headers=headers,
            data={"path": service_status_path},
            read_timeout=60,
        )
        if response.get("content"):
            return ServiceStatus.from_content(response.get("content"))
        logger.warning(f"{service_status_path} exists, but content is empty")
        return ServiceStatus()

    def use_rocklet(self) -> bool:
        if not self._nacos_provider:
            return False
        if self._nacos_provider.get_switch_status(GET_STATUS_SWITCH):
            return True
        return False
