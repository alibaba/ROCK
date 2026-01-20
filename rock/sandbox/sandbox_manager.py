import asyncio
import json
import time


from rock import env_vars
from rock.actions import (
    CommandResponse,
)
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.config import RockConfig, RuntimeConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.status import ServiceStatus
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.service.deployment_service import AbstractDeploymentService, RayDeploymentService
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.format import parse_memory_size
from rock.utils.providers import RedisProvider

logger = init_logger(__name__)


class SandboxManager(BaseManager):
    _ray_namespace: str = None
    _deployment_service: AbstractDeploymentService = None

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

        self._ray_namespace = ray_namespace
        self._deployment_service = RayDeploymentService(ray_namespace=ray_namespace)
        logger.info("sandbox service init success")

    @monitor_sandbox_operation()
    async def start_async(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxStartResponse:
        return await self.submit(config, user_info)

    @monitor_sandbox_operation()
    async def submit(self, config: DeploymentConfig, user_info: dict = {}):
        deployment_config: DeploymentConfig = await self.deployment_manager.init_config(config)
        sandbox_id = deployment_config.container_name
        # deployment: AbstractDeployment = deployment_config.get_deployment()
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
        logger.info(f"stop sandbox {sandbox_id}")
        # deployment: AbstractDeployment = await self._deployment_service.stop(sandbox_id)
        # if deployment is None:
        #     await self._clear_redis_keys(sandbox_id)
        #     raise Exception(f"sandbox {sandbox_id} not found to stop")
        # logger.info(f"start to stop run time {sandbox_id}")
        # await deployment.stop(sandbox_id)
        await self._deployment_service.stop(sandbox_id)

        try:
            self._sandbox_meta.pop(sandbox_id)
        except KeyError:
            logger.debug(f"{sandbox_id} key not found")
        logger.info(f"sandbox {sandbox_id} stopped")
        await self._clear_redis_keys(sandbox_id)

    async def get_mount(self, sandbox_id):
        deployment: AbstractDeployment = await self._deployment_service.get_deployment(sandbox_id)
        if deployment is None:
            raise Exception(f"sandbox {sandbox_id} not found to get mount")
        return deployment.get_mount()

    @monitor_sandbox_operation()
    async def commit(self, sandbox_id, image_tag: str, username: str, password: str) -> CommandResponse:
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
        response: SandboxStatusResponse = await self._deployment_service.get_status(sandbox_id)
        sandbox_info: SandboxInfo = self.get_info_from_response(response)
        if self._redis_provider:
            await self._redis_provider.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            await self._update_expire_time(sandbox_id)
            # logger.info(f"sandbox {sandbox_id} status is {remote_status}, write to redis")
        return response
    
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
            if not await self._is_deployment_alive(sandbox_id):
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
