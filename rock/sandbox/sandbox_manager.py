import asyncio
import time

import ray
from fastapi import UploadFile

from rock import env_vars
from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.request import Action
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.config import RockConfig
from rock.deployments.config import DeploymentConfig
from rock.deployments.constants import Status
from rock.deployments.status import PhaseStatus, ServiceStatus
from rock.logger import init_logger
from rock.rocklet import __version__ as swe_version
from rock.rocklet.proto.request import BashInterruptAction
from rock.rocklet.proto.request import InternalCloseBashSessionRequest as CloseBashSessionRequest
from rock.rocklet.proto.request import InternalCommand as Command
from rock.rocklet.proto.request import InternalCreateSessionRequest as CreateSessionRequest
from rock.rocklet.proto.request import InternalReadFileRequest as ReadFileRequest
from rock.rocklet.proto.request import InternalWriteFileRequest as WriteFileRequest
from rock.sandbox import __version__ as gateway_version
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.sandbox_actor import SandboxActor
from rock.utils import SANDBOX_ID
from rock.utils.providers import RedisProvider

logger = init_logger(__name__)


class SandboxManager(BaseManager):
    _ray_namespace: str = None

    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        enable_runtime_auto_clear: bool = False,
    ):
        super().__init__(
            rock_config, redis_provider=redis_provider, enable_runtime_auto_clear=enable_runtime_auto_clear
        )

        self.init_service_status()
        self._ray_namespace = ray_namespace
        logger.info("sandbox service init success")

    async def async_ray_get(self, ray_future: ray.ObjectRef):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, lambda r: ray.get(r, timeout=60), ray_future)
        except Exception as e:
            logger.error("ray get failed", exc_info=e)
            self._service_status.update_status("ray_schedule", Status.FAILED, message="ray get failed")
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get failed, {str(e)}"
            raise Exception(error_msg)
        return result

    async def async_ray_get_actor(self, sandbox_id: str):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor, ray.get_actor, self.deployment_manager.get_actor_name(sandbox_id), self._ray_namespace
            )
        except Exception as e:
            logger.error("ray get actor failed", exc_info=e)
            self._service_status.update_status("ray_schedule", Status.FAILED, message="ray get actor failed")
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get actor failed, {str(e)}"
            raise Exception(error_msg)
        return result

    @monitor_sandbox_operation()
    async def start_async(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxStartResponse:
        docker_deployment_config = await self.deployment_manager.init_config(config)

        sandbox_id = docker_deployment_config.container_name
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)

        deployment = docker_deployment_config.get_deployment()

        sandbox_actor: SandboxActor = await deployment.creator_actor(actor_name)
        sandbox_actor.start.remote()
        sandbox_actor.set_user_id.remote(user_info.get("user_id", "default"))
        sandbox_actor.set_experiment_id.remote(user_info.get("experiment_id", "default"))
        self._sandbox_meta[sandbox_id] = {"image": docker_deployment_config.image}
        logger.info(f"sandbox {sandbox_id} is submitted")
        stop_time = str(int(time.time()) + docker_deployment_config.auto_clear_time * 60)
        auto_clear_time_dict = {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(docker_deployment_config.auto_clear_time),
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: stop_time,
        }
        if self._redis_provider:
            sandbox_info = {
                SANDBOX_ID: sandbox_id,
                "user_id": await sandbox_actor.user_id.remote(),
                "experiment_id": await sandbox_actor.experiment_id.remote(),
            }
            await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            await self._redis_provider.json_set(timeout_sandbox_key(sandbox_id), "$", auto_clear_time_dict)
        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=await self.async_ray_get(sandbox_actor.host_name.remote()),
            host_ip=await self.async_ray_get(sandbox_actor.host_ip.remote()),
        )

    @monitor_sandbox_operation()
    async def start(self, config: DeploymentConfig) -> SandboxStartResponse:
        docker_deployment_config = await self.deployment_manager.init_config(config)

        sandbox_id = docker_deployment_config.container_name
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        deployment = docker_deployment_config.get_deployment()

        sandbox_actor: SandboxActor = await deployment.creator_actor(actor_name)

        await self.async_ray_get(sandbox_actor.start.remote())
        logger.info(f"sandbox {sandbox_id} is started")

        while not await self._is_actor_alive(sandbox_id):
            logger.debug(f"wait actor for sandbox alive, sandbox_id: {sandbox_id}")
            # TODO: timeout check
            await asyncio.sleep(1)
        await self.get_status(sandbox_id)

        self._sandbox_meta[sandbox_id] = {"image": docker_deployment_config.image}

        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=await self.async_ray_get(sandbox_actor.host_name.remote()),
            host_ip=await self.async_ray_get(sandbox_actor.host_ip.remote()),
        )

    @monitor_sandbox_operation()
    async def stop(self, sandbox_id):
        logger.info(f"stop sandbox {sandbox_id}")
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            await self._clear_redis_keys(sandbox_id)
            raise Exception(f"sandbox {sandbox_id} not found to stop")
        logger.info(f"start to stop run time {sandbox_id}")
        await self.async_ray_get(sandbox_actor.stop.remote())
        logger.info(f"run time stop over {sandbox_id}")
        ray.kill(sandbox_actor)
        try:
            self._sandbox_meta.pop(sandbox_id)
        except KeyError:
            logger.debug(f"{sandbox_id} key not found")
        logger.info(f"sandbox {sandbox_id} stopped")
        await self._clear_redis_keys(sandbox_id)

    async def get_mount(self, sandbox_id):
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            await self._clear_redis_keys(sandbox_id)
            raise Exception(f"sandbox {sandbox_id} not found to get mount")
        result = await self.async_ray_get(sandbox_actor.get_mount.remote())
        logger.info(f"get_mount: {result}")
        return result

    @monitor_sandbox_operation()
    async def commit(self, sandbox_id, image_tag: str, username: str, password: str) -> CommandResponse:
        logger.info(f"commit sandbox {sandbox_id}")
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            await self._clear_redis_keys(sandbox_id)
            raise Exception(f"sandbox {sandbox_id} not found to commit")
        logger.info(f"begin to commit {sandbox_id} to {image_tag}")
        result = await self.async_ray_get(sandbox_actor.commit.remote(image_tag, username, password))
        logger.info(f"commit {sandbox_id} to {image_tag} finished, result {result}")
        return result

    async def _clear_redis_keys(self, sandbox_id):
        if self._redis_provider:
            await self._redis_provider.json_delete(alive_sandbox_key(sandbox_id))
            await self._redis_provider.json_delete(timeout_sandbox_key(sandbox_id))
            logger.info(f"sandbox {sandbox_id} deleted from redis")

    @monitor_sandbox_operation()
    async def get_status(self, sandbox_id) -> SandboxStatusResponse:
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to get status")
        else:
            remote_status: ServiceStatus = await self.async_ray_get(sandbox_actor.get_status.remote())
            self.update_service_status(remote_status.phases)
            host_name = await self.async_ray_get(sandbox_actor.host_name.remote())
            alive = await self.async_ray_get(sandbox_actor.is_alive.remote())
            host_ip = await self.async_ray_get(sandbox_actor.host_ip.remote())
            config = await self.async_ray_get(sandbox_actor.deployment_config.remote())
            image = getattr(config, "image", "local")
            try:
                user_id = await sandbox_actor.user_id.remote()
                experiment_id = await sandbox_actor.experiment_id.remote()
            except Exception as e:
                logger.warning("get user_id and experiment_id failed", exc_info=e)
                user_id = "default"
                experiment_id = "default"
            if alive and self._redis_provider:
                status_dict = remote_status.to_dict()
                status_dict["host_ip"] = host_ip
                status_dict[SANDBOX_ID] = sandbox_id
                status_dict["image"] = image
                status_dict["user_id"] = user_id
                status_dict["experiment_id"] = experiment_id
                await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", status_dict)
                await self._update_expire_time(sandbox_id)
                logger.info(f"sandbox {sandbox_id} status is {remote_status}, write to redis")
            return SandboxStatusResponse(
                sandbox_id=sandbox_id,
                status=self._service_status.phases,
                port_mapping=remote_status.get_port_mapping(),
                host_name=host_name,
                host_ip=host_ip,
                is_alive=alive.is_alive,
                image=image,
                swe_rex_version=swe_version,
                gateway_version=gateway_version,
                user_id=user_id,
                experiment_id=experiment_id,
            )

    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        sandbox_actor = await self.async_ray_get_actor(request.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.container_name} not found to create session")
        await self._update_expire_time(request.container_name)
        return await self.async_ray_get(sandbox_actor.create_session.remote(request))

    @monitor_sandbox_operation()
    async def run_in_session(self, action: Action | BashInterruptAction) -> BashObservation:
        sandbox_actor = await self.async_ray_get_actor(action.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {action.container_name} not found to run in session")
        await self._update_expire_time(action.container_name)
        return await self.async_ray_get(sandbox_actor.run_in_session.remote(action))

    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        sandbox_actor = await self.async_ray_get_actor(request.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.container_name} not found to close session")
        await self._update_expire_time(request.container_name)
        return await self.async_ray_get(sandbox_actor.close_session.remote(request))

    async def execute(self, command: Command) -> CommandResponse:
        sandbox_actor = await self.async_ray_get_actor(command.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {command.container_name} not found to execute")
        await self._update_expire_time(command.container_name)
        return await self.async_ray_get(sandbox_actor.execute.remote(command))

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        sandbox_actor = await self.async_ray_get_actor(request.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.container_name} not found to read file")
        await self._update_expire_time(request.container_name)
        return await self.async_ray_get(sandbox_actor.read_file.remote(request))

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        sandbox_actor = await self.async_ray_get_actor(request.container_name)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.container_name} not found to write file")
        await self._update_expire_time(request.container_name)
        return await self.async_ray_get(sandbox_actor.write_file.remote(request))

    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to upload file")
        await self._update_expire_time(sandbox_id)
        return await self.async_ray_get(sandbox_actor.upload.remote(file, target_path))

    async def _is_expired(self, sandbox_id):
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found")
        timeout_dict = await self._redis_provider.json_get(timeout_sandbox_key(sandbox_id), "$")
        if timeout_dict is None or len(timeout_dict) == 0:
            raise Exception(f"sandbox {sandbox_id} timeout key not found")

        if timeout_dict is not None and len(timeout_dict) > 0:
            expire_time: int = int(timeout_dict[0].get(env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY))
            return int(time.time()) > expire_time
        else:
            logger.info(f"sandbox_id:[{sandbox_id}] is already cleared")
            return True

    async def _is_actor_alive(self, sandbox_id):
        try:
            actor = await self.async_ray_get_actor(sandbox_id)
            return actor is not None
        except Exception as e:
            logger.error("get actor failed", exc_info=e)
            return False

    async def _check_job_background(self):
        if not self._redis_provider:
            return
        logger.info("check job background")
        async for key in self._redis_provider.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=100):
            sandbox_id = key.removeprefix(ALIVE_PREFIX)
            if not await self._is_actor_alive(sandbox_id):
                logger.info(f"sandbox_id:[{sandbox_id}] is not alive, start to stop")
                await self._clear_redis_keys(sandbox_id)
                continue

            try:
                is_expired = await self._is_expired(sandbox_id)
                if is_expired:
                    logger.info(f"sandbox_id:[{sandbox_id}] is expired, start to stop")
                    asyncio.create_task(self.stop(sandbox_id))
            except asyncio.CancelledError as e:
                logger.error("check_job_background CancelledError", exc_info=e)
                continue
            except Exception as e:
                logger.error("check_job_background Exception", exc_info=e)
                continue

    def init_service_status(self) -> None:
        self._service_status = ServiceStatus()
        self._service_status.add_phase(
            "ray_schedule", PhaseStatus(status=Status.RUNNING, message="ray schedule running")
        )

    def update_service_status(self, remote_status: dict) -> dict:
        for key, value in remote_status.items():
            self._service_status.update_status(key, value.status, value.message)
        return self._service_status

    async def get_sandbox_statistics(self, sandbox_id):
        sandbox_actor = await self.async_ray_get_actor(sandbox_id)
        resource_metrics = await self.async_ray_get(sandbox_actor.get_sandbox_statistics.remote())
        return resource_metrics

    async def _update_expire_time(self, sandbox_id):
        if self._redis_provider is None:
            return
        sandbox_status_dict = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if not sandbox_status_dict or len(sandbox_status_dict) == 0:
            logger.info(f"sandbox-{sandbox_id} is not alive, skip update expire time")
            return
        origin_info = await self._redis_provider.json_get(timeout_sandbox_key(sandbox_id), "$")
        if origin_info is None or len(origin_info) == 0:
            logger.info(f"sandbox-{sandbox_id} is not initialized, skip update expire time")
            return
        auto_clear_time: str = origin_info[0].get(env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY)
        expire_time: int = int(time.time()) + int(auto_clear_time) * 60
        logger.info(f"sandbox-{sandbox_id} update expire time: {expire_time}")
        new_dict = {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: auto_clear_time,
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(expire_time),
        }
        await self._redis_provider.json_set(timeout_sandbox_key(sandbox_id), "$", new_dict)
