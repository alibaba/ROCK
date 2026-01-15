import asyncio
import logging
import time

import pytest
import ray
from fakeredis import aioredis

from rock.actions import SandboxStatusResponse
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig, RayDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.providers.redis_provider import RedisProvider

logger = logging.getLogger(__file__)

@pytest.fixture
async def redis_provider():
    provider = RedisProvider(host=None, port=None, password="")
    provider.client = aioredis.FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


@pytest.fixture
async def sandbox_manager(rock_config: RockConfig, redis_provider: RedisProvider, ray_init_shutdown):
    sandbox_manager = SandboxManager(
        rock_config,
        redis_provider=redis_provider,
        ray_namespace=rock_config.ray.namespace,
        enable_runtime_auto_clear=rock_config.runtime.enable_auto_clear,
    )
    return sandbox_manager


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_async_sandbox_start(sandbox_manager: SandboxManager):
    response = await sandbox_manager.start_async(DockerDeploymentConfig())
    sandbox_id = response.sandbox_id
    assert sandbox_id is not None
    search_start_time = time.time()
    while time.time() - search_start_time < 60:
        is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
        if is_alive_response:
            break

    is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
    assert is_alive_response

    sandbox_actor = await sandbox_manager.async_ray_get_actor(sandbox_id)
    assert sandbox_actor is not None
    assert await sandbox_actor.user_id.remote() == "default"
    assert await sandbox_actor.experiment_id.remote() == "default"

    await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status(sandbox_manager):
    response = await sandbox_manager.start_async(DockerDeploymentConfig(image="python:3.11"))
    await asyncio.sleep(5)
    docker_status: SandboxStatusResponse = await sandbox_manager.get_status(response.sandbox_id)
    assert docker_status.status["docker_run"]
    assert docker_status.status["image_pull"]
    # wait to ensure that sandbox is alive(runtime ready)
    await asyncio.sleep(60)
    docker_status: SandboxStatusResponse = await sandbox_manager.get_status(response.sandbox_id)
    assert docker_status.is_alive
    assert len(docker_status.port_mapping) == 3
    assert docker_status.port_mapping[Port.SSH]
    assert docker_status.host_ip
    assert docker_status.host_name
    assert docker_status.image == "python:3.11"
    resource_metrics = await sandbox_manager.get_sandbox_statistics(response.sandbox_id)
    print(resource_metrics)
    await sandbox_manager.stop(response.sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_ray_actor_is_alive(sandbox_manager):
    docker_deploy_config = DockerDeploymentConfig()

    response = await sandbox_manager.start_async(docker_deploy_config)
    assert response.sandbox_id is not None

    assert await sandbox_manager._is_actor_alive(response.sandbox_id)

    sandbox_actor = await sandbox_manager.async_ray_get_actor(response.sandbox_id)
    ray.kill(sandbox_actor)

    assert not await sandbox_manager._is_actor_alive(response.sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_user_info_set_success(sandbox_manager):
    user_info = {"user_id": "test_user_id", "experiment_id": "test_experiment_id"}
    response = await sandbox_manager.start_async(RayDeploymentConfig(), user_info=user_info)
    sandbox_id = response.sandbox_id

    cnt = 0
    while True:
        is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
        if is_alive_response:
            break
        time.sleep(1)
        cnt += 1
        if cnt > 60:
            raise Exception("sandbox not alive")

    is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
    assert is_alive_response

    sandbox_actor = await sandbox_manager.async_ray_get_actor(sandbox_id)
    assert sandbox_actor is not None
    assert await sandbox_actor.user_id.remote() == "test_user_id"
    assert await sandbox_actor.experiment_id.remote() == "test_experiment_id"

    await sandbox_manager.stop(sandbox_id)


def test_set_sandbox_status_response():
    service_status = ServiceStatus()
    status_response = SandboxStatusResponse(sandbox_id="test", status=service_status.phases)
    assert status_response.sandbox_id == "test"


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_resource_limit_exception(sandbox_manager, docker_deployment_config):
    docker_deployment_config.cpus = 20
    with pytest.raises(BadRequestRockError) as e:
        await sandbox_manager.start_async(docker_deployment_config)
    logger.warning(f"Resource limit exception: {str(e)}", exc_info=True)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_resource_limit_exception_memory(sandbox_manager, docker_deployment_config):
    docker_deployment_config.memory = "65g"
    with pytest.raises(BadRequestRockError) as e:
        await sandbox_manager.start_async(docker_deployment_config)
    logger.warning(f"Resource limit exception: {str(e)}", exc_info=True)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_system_resource_info(sandbox_manager):
    total_cpu, total_mem, ava_cpu, ava_mem = await sandbox_manager._collect_system_resource_metrics()
    assert total_cpu > 0
    assert total_mem > 0
    assert ava_cpu > 0
    assert ava_mem > 0
