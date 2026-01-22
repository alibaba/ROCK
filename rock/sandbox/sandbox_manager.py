import asyncio
import time
from fastapi import UploadFile

from rock import env_vars
from rock.actions import (
    BashObservation,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.response import IsAliveResponse, State
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxAction as Action

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.config import RockConfig, RuntimeConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.status import PersistedServiceStatus, ServiceStatus
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig

from rock.logger import init_logger
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.service.deployment_service import AbstractDeploymentService, RayDeploymentService
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils import (
    EAGLE_EYE_TRACE_ID,
    HttpUtils,
    trace_id_ctx_var,
)
from rock.utils.format import parse_memory_size
<<<<<<< HEAD
from rock.utils.providers.redis_provider import RedisProvider
from rock.utils.service import build_sandbox_from_redis
=======
from rock.utils.providers import RedisProvider
from rock.admin.core.ray_service import RayService
from rock.rocklet import __version__ as swe_version
from rock.sandbox import __version__ as gateway_version
>>>>>>> ddca701 (fix test case: add rock auth in get status)

logger = init_logger(__name__)


class SandboxManager(BaseManager):
    _ray_namespace: str = None
    _deployment_service: AbstractDeploymentService = None
    _proxy_service: SandboxProxyService = None

    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        ray_service: RayService | None = None,
        enable_runtime_auto_clear: bool = False,
    ):
        super().__init__(
            rock_config, redis_provider=redis_provider, enable_runtime_auto_clear=enable_runtime_auto_clear
        )
        self._ray_service = ray_service
        self._ray_namespace = ray_namespace
        self._deployment_service = RayDeploymentService(ray_namespace=ray_namespace)
        self._proxy_service = SandboxProxyService(rock_config, redis_provider)
        logger.info("sandbox service init success")

    
    @monitor_sandbox_operation()
    async def start_async(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxStartResponse:
        return await self.submit(config, user_info)

    @monitor_sandbox_operation()
    async def submit(self, config: DeploymentConfig, user_info: dict = {}):
        async with self._ray_service.get_ray_rwlock().read_lock():
            deployment_config: DeploymentConfig = await self.deployment_manager.init_config(config)
            sandbox_id = deployment_config.container_name
            self.validate_sandbox_spec(self.rock_config.runtime, config)
            self._sandbox_meta[sandbox_id] = {"image": deployment_config.image}
            sandbox_info: SandboxInfo = await self._deployment_service.submit(deployment_config, user_info)
            logger.info(f"sandbox {sandbox_id} is submitted")

            stop_time = str(int(time.time()) + deployment_config.auto_clear_time * 60)
            auto_clear_time_dict = {
                env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(deployment_config.auto_clear_time),
                env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: stop_time,
            }
            if self._redis_provider:
                await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
                await self._redis_provider.json_set(timeout_sandbox_key(sandbox_id), "$", auto_clear_time_dict)

            return SandboxStartResponse(
                sandbox_id=sandbox_id,
                host_name=sandbox_info.get("host_name"),
                host_ip=sandbox_info.get("host_ip"),
            )

    @monitor_sandbox_operation()
    async def stop(self, sandbox_id):
        async with self._ray_service.get_ray_rwlock().read_lock():
            logger.info(f"stop sandbox {sandbox_id}")
            try:
                await self._deployment_service.stop(sandbox_id)
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
            return self._deployment_service.get_mount(sandbox_id)

    @monitor_sandbox_operation()
    async def commit(self, sandbox_id, image_tag: str, username: str, password: str) -> CommandResponse:
        async with self._ray_service.get_ray_rwlock().read_lock():
            logger.info(f"commit sandbox {sandbox_id}")
            deployment: AbstractDeployment = await self._deployment_service.get_deployment(sandbox_id)
            if deployment is None:
                await self._clear_redis_keys(sandbox_id)
                raise Exception(f"sandbox {sandbox_id} not found to commit")
            logger.info(f"begin to commit {sandbox_id} to {image_tag}")
            result = await deployment.commit(image_tag, username, password)
            logger.info(f"commit {sandbox_id} to {image_tag} finished, result {result}")
            return result

    async def _clear_redis_keys(self, sandbox_id):
        if self._redis_provider:
            await self._redis_provider.json_delete(alive_sandbox_key(sandbox_id))
            await self._redis_provider.json_delete(timeout_sandbox_key(sandbox_id))
            logger.info(f"sandbox {sandbox_id} deleted from redis")

    @monitor_sandbox_operation()
    async def get_status(self, sandbox_id) -> SandboxStatusResponse:
        async with self._ray_service.get_ray_rwlock().read_lock():
            deployment_info: SandboxInfo = await self._deployment_service.get_status(sandbox_id)
            sandbox_info: SandboxInfo = None
            if self._redis_provider:
                sandbox_info = await self.build_sandbox_info_from_redis(sandbox_id)
                if sandbox_info is None:
                    sandbox_info = deployment_info
                else:
                    sandbox_info["state"] = deployment_info.get("state")
                await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
                await self._update_expire_time(sandbox_id)
                remote_info = {k: v for k, v in deployment_info.items() if k in ['status', 'port_mapping', 'alive']}
                sandbox_info.update(remote_info)
                logger.info(f"sandbox {sandbox_id} status is {sandbox_info}, write to redis")
            else:
                sandbox_info = deployment_info

<<<<<<< HEAD
    async def _get_sandbox_info(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox info, prioritize Redis, fallback to Ray Actor"""
        if self._redis_provider:
            sandbox_info = await build_sandbox_from_redis(self._redis_provider, sandbox_id)
        else:
            sandbox_actor = await self.async_ray_get_actor(sandbox_id)
            if sandbox_actor is None:
                raise Exception(f"sandbox {sandbox_id} not found to get status")
            sandbox_info = await self.async_ray_get(sandbox_actor.sandbox_info.remote())

        if sandbox_info is None:
            raise Exception(f"sandbox {sandbox_id} not found to get status")

        return sandbox_info

    async def _check_alive_status(
    self, sandbox_id: str, host_ip: str, remote_status: ServiceStatus
) -> bool:
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

    @monitor_sandbox_operation()
    async def get_status_v2(self, sandbox_id) -> SandboxStatusResponse:
        # 1. Get sandbox_info (unified exception handling)
        sandbox_info = await self._get_sandbox_info(sandbox_id)

        # 2. Parallel execution: update expire time & get remote status
        host_ip = sandbox_info.get("host_ip")
        _, remote_status = await asyncio.gather(
            self._update_expire_time(sandbox_id),
            self.get_remote_status(sandbox_id, host_ip),
        )

        # 3. Update sandbox_info and check alive status
        sandbox_info.update(remote_status.to_dict())
        is_alive = await self._check_alive_status(sandbox_id, host_ip, remote_status)
        if is_alive:
            sandbox_info["state"] = State.RUNNING

        # 4. Persist to Redis if Redis exists
        if self._redis_provider:
            await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            logger.info(f"sandbox {sandbox_id} status is {remote_status}, write to redis")

        # 5. Build and return response
        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            status=remote_status.phases,
            port_mapping=remote_status.get_port_mapping(),
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

    async def get_remote_status(self, sandbox_id: str, host_ip: str) -> ServiceStatus:
        service_status_path = PersistedServiceStatus.gen_service_status_path(sandbox_id)
        worker_rocklet_port = env_vars.ROCK_WORKER_ROCKLET_PORT if env_vars.ROCK_WORKER_ROCKLET_PORT else Port.PROXY
        execute_url = f"http://{host_ip}:{worker_rocklet_port}/execute"
        read_file_url = f"http://{host_ip}:{worker_rocklet_port}/read_file"
        headers={"sandbox_id": sandbox_id, EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get()}
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
        error_msg = (
            f"get_remote_status failed! {response.get('failure_reason') if response.get('failure_reason') else ''}"
        )
        raise Exception(error_msg)    

    def get_info_from_response(self, response: SandboxStatusResponse) -> SandboxInfo:
        return SandboxInfo(
            host_name=response.host_name,
            host_ip=response.host_ip,
            user_id=response.user_id,
            experiment_id=response.experiment_id,
            namespace=response.namespace,
            sandbox_id=response.sandbox_id,
            cpus=response.cpus,
            memory=response.memory,
            port_mapping=response.port_mapping,
        )
=======
            return SandboxStatusResponse(
                sandbox_id=sandbox_id,
                status=sandbox_info.get("status"),
                state=sandbox_info.get("state"),
                port_mapping=sandbox_info.get("port_mapping"),
                host_name=sandbox_info.get("host_name"),
                host_ip=sandbox_info.get("host_ip"),
                is_alive=sandbox_info.get("alive"),
                image=sandbox_info.get("image"),
                swe_rex_version=swe_version,
                gateway_version=gateway_version,
                user_id=sandbox_info.get("user_id"),
                experiment_id=sandbox_info.get("experiment_id"),
                namespace=sandbox_info.get("namespace"),
                cpus=sandbox_info.get("cpus"),
                memory=sandbox_info.get("memory"),
            )

    async def build_sandbox_info_from_redis(self, sandbox_id: str) -> SandboxInfo | None:
        if self._redis_provider:
            sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
            if sandbox_status and len(sandbox_status) > 0:
                return sandbox_status[0]
        return None
>>>>>>> ddca701 (fix test case: add rock auth in get status)
    
    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        return await self._proxy_service.create_session(request)

    @monitor_sandbox_operation()
    async def run_in_session(self, action: Action) -> BashObservation:
        return await self._proxy_service.run_in_session(action)

    async def execute(self, command: Command) -> CommandResponse:
        return await self._proxy_service.execute(command)
    
    # TODO:remain for test, delete it after test refactor
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        return await self._proxy_service.read_file(request)

    # TODO:remain for test, delete it after test refactor
    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        return await self._proxy_service.write_file(request)

    # TODO:remain for test, delete it after test refactor
    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        return await self._proxy_service.upload(file, target_path, sandbox_id)

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

    async def _is_deployment_alive(self, sandbox_id):
        try:
            deployment = await self._deployment_service.get_deployment(sandbox_id)
            return deployment is not None
        except Exception as e:
            logger.error("get deployment failed", exc_info=e)
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
        resource_metrics = await self._deployment_service.get_sandbox_statistics(sandbox_id)
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
            raise BadRequestRockError(f"Invalid memory size: {deployment_config.memory}")
