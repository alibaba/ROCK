import asyncio
import logging
import time
import uuid

import pytest
import ray

from rock.actions import SandboxStatusResponse
from rock.actions.sandbox.response import State
from rock.deployments.config import DockerDeploymentConfig, RayDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError
from tests.unit.conftest import check_sandbox_status_until_alive

logger = logging.getLogger(__file__)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_async_sandbox_start(sandbox_manager: SandboxManager):
    response = await sandbox_manager.submit(DockerDeploymentConfig())
    sandbox_id = response.sandbox_id
    assert sandbox_id is not None
    assert await wait_sandbox_instance_alive(sandbox_manager, sandbox_id)

    assert await sandbox_manager._deployment_service.is_alive(sandbox_id)

    sandbox_status = await sandbox_manager.get_status(sandbox_id)
    assert sandbox_status.user_id == "default"
    assert sandbox_status.experiment_id == "default"

    await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status(sandbox_manager):
    response = await sandbox_manager.submit(DockerDeploymentConfig(image="python:3.11"))
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

    response = await sandbox_manager.submit(docker_deploy_config)
    assert response.sandbox_id is not None

    assert await wait_sandbox_instance_alive(sandbox_manager, response.sandbox_id)

    sandbox_actor = await sandbox_manager._deployment_service._ray_service.async_ray_get_actor(response.sandbox_id)
    ray.kill(sandbox_actor)

    assert not await sandbox_manager._deployment_service.is_alive(response.sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_user_info_set_success(sandbox_manager):
    user_info = {"user_id": "test_user_id", "experiment_id": "test_experiment_id"}
    response = await sandbox_manager.submit(RayDeploymentConfig(), user_info=user_info)
    sandbox_id = response.sandbox_id

    assert await wait_sandbox_instance_alive(sandbox_manager, sandbox_id)

    is_alive_response = await sandbox_manager._deployment_service.is_alive(sandbox_id)
    assert is_alive_response

    sandbox_status = await sandbox_manager.get_status(sandbox_id)
    assert sandbox_status.user_id == "test_user_id"
    assert sandbox_status.experiment_id == "test_experiment_id"

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
        await sandbox_manager.submit(docker_deployment_config)
    logger.warning(f"Resource limit exception: {str(e)}", exc_info=True)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_resource_limit_exception_memory(sandbox_manager, docker_deployment_config):
    docker_deployment_config.memory = "65g"
    with pytest.raises(BadRequestRockError) as e:
        await sandbox_manager.submit(docker_deployment_config)
    logger.warning(f"Resource limit exception: {str(e)}", exc_info=True)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_system_resource_info(sandbox_manager):
    from rock.actions.sandbox.response import SystemResourceMetrics
    metrics: SystemResourceMetrics = await sandbox_manager._collect_system_resource_metrics()
    assert metrics.total_cpu > 0
    assert metrics.total_memory > 0
    assert metrics.available_cpu >= 0
    assert metrics.available_memory >= 0
    assert metrics.available_cpu <= metrics.total_cpu
    assert metrics.available_memory <= metrics.total_memory


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status_state(sandbox_manager):
    response = await sandbox_manager.submit(
        DockerDeploymentConfig(),
    )
    sandbox_id = response.sandbox_id
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id)
    sandbox_status = await sandbox_manager.get_status(sandbox_id)
    assert sandbox_status.state == State.RUNNING
    await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_sandbox_start_with_sandbox_id(sandbox_manager):
    try:
        sandbox_id = uuid.uuid4().hex
        response = await sandbox_manager.submit(DockerDeploymentConfig(container_name=sandbox_id))
        assert response.sandbox_id == sandbox_id
        await check_sandbox_status_until_alive(sandbox_manager, sandbox_id)
        with pytest.raises(BadRequestRockError) as e:
            await sandbox_manager.submit(
                DockerDeploymentConfig(container_name=sandbox_id),
                sandbox_id=sandbox_id,
            )
    except Exception as e:
        logger.error(f"test_sandbox_start_with_sandbox_id error: {str(e)}", exc_info=True)
    finally:
        await sandbox_manager.stop(sandbox_id)

async def wait_sandbox_instance_alive(sandbox_manager: SandboxManager, sandbox_id: str) -> bool:
    cnt = 0
    while True:
        is_alive_response = await sandbox_manager._deployment_service.is_alive(sandbox_id)
        if is_alive_response:
            return True
        time.sleep(1)
        cnt += 1
        if cnt > 60:
            raise Exception("sandbox not alive")
