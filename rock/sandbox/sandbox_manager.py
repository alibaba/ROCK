import asyncio
import time

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
from rock.actions.sandbox.response import IsAliveResponse, State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.billing import log_billing_info
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.config import RockConfig, RuntimeConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.status import PersistedServiceStatus, ServiceStatus
from rock.logger import init_logger
from rock.rocklet import __version__ as swe_version
from rock.sandbox import __version__ as gateway_version
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError
from rock.utils import (
    EAGLE_EYE_TRACE_ID,
    HttpUtils,
    trace_id_ctx_var,
)
from rock.utils.crypto_utils import AESEncryption
from rock.utils.format import convert_to_gb, parse_memory_size
from rock.utils.providers.redis_provider import RedisProvider
from rock.utils.service import build_sandbox_from_redis
from rock.utils.system import get_iso8601_timestamp

logger = init_logger(__name__)


class SandboxManager(BaseManager):
    _ray_namespace: str = None

    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        ray_service: RayService | None = None,
        enable_runtime_auto_clear: bool = False,
        operator: AbstractOperator | None = None,
    ):
        super().__init__(
            rock_config, redis_provider=redis_provider, enable_runtime_auto_clear=enable_runtime_auto_clear
        )
        self._ray_service = ray_service
        self._ray_namespace = ray_namespace
        self._operator = operator
        self._aes_encrypter = AESEncryption()
        if redis_provider:
            self._operator.set_redis_provider(redis_provider)
        logger.info("sandbox service init success")

    async def refresh_aes_key(self):
        try:
            await self.rock_config.update()
            if aes_encrypt_key := self.rock_config.proxy_service.aes_encrypt_key:
                self._aes_encrypter.key_update(aes_encrypt_key)
        except Exception as e:
            logger.error(f"update aes key failed, error: {e}")
            raise InternalServerRockError(f"update aes key failed, {str(e)}")

    async def _check_sandbox_exists_in_redis(self, config: DeploymentConfig):
        if isinstance(config, DockerDeploymentConfig) and config.container_name:
            sandbox_id = config.container_name
            if self._redis_provider and await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$"):
                raise BadRequestRockError(f"Sandbox {sandbox_id} already exists")

    def _setup_sandbox_actor_metadata(self, sandbox_actor: SandboxActor, user_info: UserInfo) -> None:
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")

        sandbox_actor.set_user_id.remote(user_id)
        sandbox_actor.set_experiment_id.remote(experiment_id)
        sandbox_actor.set_namespace.remote(namespace)

    async def _build_sandbox_info_metadata(
        self, sandbox_info: SandboxInfo, user_info: UserInfo, cluster_info: ClusterInfo
    ) -> None:
        sandbox_info["memory"] = convert_to_gb(sandbox_info.get("memory"))
        sandbox_info["user_id"] = user_info.get("user_id", "default")
        sandbox_info["experiment_id"] = user_info.get("experiment_id", "default")
        sandbox_info["namespace"] = user_info.get("namespace", "default")
        sandbox_info["cluster_name"] = cluster_info.get("cluster_name", "default")
        rock_auth = user_info.get("rock_authorization", "default")
        await self.refresh_aes_key()
        sandbox_info["rock_authorization_encrypted"] = self._aes_encrypter.encrypt(rock_auth)
        sandbox_info["state"] = State.PENDING
        sandbox_info["create_time"] = get_iso8601_timestamp()

    @monitor_sandbox_operation()
    async def start_async(
        self, config: DeploymentConfig, user_info: UserInfo = {}, cluster_info: ClusterInfo = {}
    ) -> SandboxStartResponse:
        await self._check_sandbox_exists_in_redis(config)
        self.validate_sandbox_spec(self.rock_config.runtime, config)
        docker_deployment_config: DockerDeploymentConfig = await self.deployment_manager.init_config(config)
        sandbox_id = docker_deployment_config.container_name
        sandbox_info: SandboxInfo = await self._operator.submit(docker_deployment_config, user_info)
        stop_time = str(int(time.time()) + docker_deployment_config.auto_clear_time * 60)
        auto_clear_time_dict = {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(docker_deployment_config.auto_clear_time),
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: stop_time,
        }
        await self._build_sandbox_info_metadata(sandbox_info, user_info, cluster_info)
        if self._redis_provider:
            await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            await self._redis_provider.json_set(timeout_sandbox_key(sandbox_id), "$", auto_clear_time_dict)
        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
        )

    @monitor_sandbox_operation()
    async def start(self, config: DeploymentConfig) -> SandboxStartResponse:
        docker_deployment_config: DockerDeploymentConfig = await self.deployment_manager.init_config(config)

        sandbox_id = docker_deployment_config.container_name
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        deployment = docker_deployment_config.get_deployment()

        sandbox_actor: SandboxActor = await deployment.creator_actor(actor_name)

        await self._ray_service.async_ray_get(sandbox_actor.start.remote())
        logger.info(f"sandbox {sandbox_id} is started")

        while not await self._is_actor_alive(sandbox_id):
            logger.debug(f"wait actor for sandbox alive, sandbox_id: {sandbox_id}")
            # TODO: timeout check
            await asyncio.sleep(1)
        await self.get_status(sandbox_id)

        self._sandbox_meta[sandbox_id] = {"image": docker_deployment_config.image}

        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=await self._ray_service.async_ray_get(sandbox_actor.host_name.remote()),
            host_ip=await self._ray_service.async_ray_get(sandbox_actor.host_ip.remote()),
        )

    @monitor_sandbox_operation()
    async def stop(self, sandbox_id):
        logger.info(f"stop sandbox {sandbox_id}")
        sandbox_info: SandboxInfo = await build_sandbox_from_redis(self._redis_provider, sandbox_id)
        if sandbox_info and sandbox_info.get("start_time"):
            sandbox_info["stop_time"] = get_iso8601_timestamp()
            log_billing_info(sandbox_info=sandbox_info)
        try:
            await self._operator.stop(sandbox_id)
        except ValueError as e:
            logger.error(f"ray get actor, actor {sandbox_id} not exist", exc_info=e)
            await self._clear_redis_keys(sandbox_id)
        try:
            self._sandbox_meta.pop(sandbox_id)
        except KeyError:
            logger.debug(f"{sandbox_id} key not found")
        logger.info(f"sandbox {sandbox_id} stopped")
        await self._clear_redis_keys(sandbox_id)

    async def get_mount(self, sandbox_id):
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            if sandbox_actor is None:
                await self._clear_redis_keys(sandbox_id)
                raise Exception(f"sandbox {sandbox_id} not found to get mount")
            result = await self._ray_service.async_ray_get(sandbox_actor.get_mount.remote())
            logger.info(f"get_mount: {result}")
            return result

    @monitor_sandbox_operation()
    async def commit(self, sandbox_id, image_tag: str, username: str, password: str) -> CommandResponse:
        async with self._ray_service.get_ray_rwlock().read_lock():
            logger.info(f"commit sandbox {sandbox_id}")
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            if sandbox_actor is None:
                await self._clear_redis_keys(sandbox_id)
                raise Exception(f"sandbox {sandbox_id} not found to commit")
            logger.info(f"begin to commit {sandbox_id} to {image_tag}")
            result = await self._ray_service.async_ray_get(sandbox_actor.commit.remote(image_tag, username, password))
            logger.info(f"commit {sandbox_id} to {image_tag} finished, result {result}")
            return result

    async def _clear_redis_keys(self, sandbox_id):
        if self._redis_provider:
            await self._redis_provider.json_delete(alive_sandbox_key(sandbox_id))
            await self._redis_provider.json_delete(timeout_sandbox_key(sandbox_id))
            logger.info(f"sandbox {sandbox_id} deleted from redis")

    @monitor_sandbox_operation()
    async def get_status(self, sandbox_id, use_rocklet: bool = False) -> SandboxStatusResponse:
        if use_rocklet and self._redis_provider:
            sandbox_info: SandboxInfo = await build_sandbox_from_redis(self._redis_provider, sandbox_id)
            host_ip = sandbox_info.get("host_ip")
            remote_status = await self.get_remote_status(sandbox_id, host_ip)
            is_alive = await self._check_alive_status(sandbox_id, host_ip, remote_status)
            sandbox_info.update(remote_status.to_dict())
        else:
            sandbox_info: SandboxInfo = await self._operator.get_status(sandbox_id=sandbox_id)
            is_alive = sandbox_info.get("state") == State.RUNNING
        self._update_sandbox_alive_info(sandbox_info, is_alive)
        if self._redis_provider:
            await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            (self._update_expire_time(sandbox_id),)
        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            status=sandbox_info.get("phases"),
            port_mapping=sandbox_info.get("port_mapping"),
            state=sandbox_info.get("state"),
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
            is_alive=is_alive,
            image=sandbox_info.get("image"),
            swe_rex_version=swe_version,
            gateway_version=gateway_version,
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
        )

    async def build_sandbox_info_from_redis(self, sandbox_id: str, deployment_info: SandboxInfo) -> SandboxInfo | None:
        sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if sandbox_status and len(sandbox_status) > 0:
            sandbox_info = sandbox_status[0]
            remote_info = {
                k: v for k, v in deployment_info.items() if k in ["phases", "port_mapping", "alive", "state"]
            }
            if "phases" in remote_info and remote_info["phases"]:
                remote_info["phases"] = {name: phase.to_dict() for name, phase in remote_info["phases"].items()}
            sandbox_info.update(remote_info)
        else:
            sandbox_info = deployment_info
        return sandbox_info

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

    def _update_sandbox_alive_info(self, sandbox_info: SandboxInfo, is_alive: bool) -> None:
        if is_alive:
            sandbox_info["state"] = State.RUNNING
            # Set start_time for the first time the sandbox becomes alive
            if sandbox_info.get("start_time") is None:
                sandbox_info["start_time"] = get_iso8601_timestamp()

    async def get_status_v2(self, sandbox_id) -> SandboxStatusResponse:
        """
        Deprecated: Use get_status(sandbox_id, use_rocklet=True) instead.
        This method is kept for backward compatibility.
        """
        return await self.get_status(sandbox_id, use_rocklet=True)

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

    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        actor_name = self.deployment_manager.get_actor_name(request.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.sandbox_id} not found to create session")
        await self._update_expire_time(request.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.create_session.remote(request))

    @monitor_sandbox_operation()
    async def run_in_session(self, action: Action) -> BashObservation:
        actor_name = self.deployment_manager.get_actor_name(action.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {action.sandbox_id} not found to run in session")
        await self._update_expire_time(action.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.run_in_session.remote(action))

    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        actor_name = self.deployment_manager.get_actor_name(request.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.sandbox_id} not found to close session")
        await self._update_expire_time(request.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.close_session.remote(request))

    async def execute(self, command: Command) -> CommandResponse:
        actor_name = self.deployment_manager.get_actor_name(command.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {command.sandbox_id} not found to execute")
        await self._update_expire_time(command.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.execute.remote(command))

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        actor_name = self.deployment_manager.get_actor_name(request.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.sandbox_id} not found to read file")
        await self._update_expire_time(request.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.read_file.remote(request))

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        actor_name = self.deployment_manager.get_actor_name(request.sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {request.sandbox_id} not found to write file")
        await self._update_expire_time(request.sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.write_file.remote(request))

    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        if sandbox_actor is None:
            raise Exception(f"sandbox {sandbox_id} not found to upload file")
        await self._update_expire_time(sandbox_id)
        return await self._ray_service.async_ray_get(sandbox_actor.upload.remote(file, target_path))

    async def _is_expired(self, sandbox_id):
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
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            return actor is not None
        except Exception as e:
            logger.error("get actor failed", exc_info=e)
            return False

    async def _check_job_background(self):
        if not self._redis_provider:
            return
        logger.debug("check job background")
        async for key in self._redis_provider.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=100):
            sandbox_id = key.removeprefix(ALIVE_PREFIX)
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

    async def get_sandbox_statistics(self, sandbox_id):
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        resource_metrics = await self._ray_service.async_ray_get(sandbox_actor.get_sandbox_statistics.remote())
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

    def validate_sandbox_spec(self, runtime_config: RuntimeConfig, deployment_config: DeploymentConfig) -> None:
        try:
            memory = parse_memory_size(deployment_config.memory)
            max_memory = parse_memory_size(runtime_config.max_allowed_spec.memory)
            if deployment_config.cpus > runtime_config.max_allowed_spec.cpus:
                raise BadRequestRockError(
                    f"Requested CPUs {deployment_config.cpus} exceed the maximum allowed {runtime_config.max_allowed_spec.cpus}"
                )
            if memory > max_memory:
                raise BadRequestRockError(
                    f"Requested memory {deployment_config.memory} exceed the maximum allowed {runtime_config.max_allowed_spec.memory}"
                )
        except ValueError as e:
            logger.warning(f"Invalid memory size: {deployment_config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {self._config.memory}")
