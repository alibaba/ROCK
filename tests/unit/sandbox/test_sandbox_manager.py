import asyncio
import logging
import time
import uuid

import pytest
import ray

from rock.actions import SandboxStatusResponse
from rock.actions.sandbox.response import State
from rock.config import RockConfig
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

    actor_name = sandbox_manager._deployment_service._get_actor_name(response.sandbox_id)
    sandbox_actor = await sandbox_manager._deployment_service._ray_service.async_ray_get_actor(actor_name)
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


async def wait_for_rocklet_service_ready(sandbox_manager: SandboxManager, sandbox_id: str, timeout: int = 120):
    """Wait for rocklet HTTP service to be ready in container
    
    Args:
        sandbox_manager: SandboxManager instance
        sandbox_id: Sandbox ID
        timeout: Maximum wait time in seconds
        
    Raises:
        Exception: If service is not ready within timeout
    """
    from rock.deployments.constants import Port
    from rock.utils import HttpUtils, EAGLE_EYE_TRACE_ID, trace_id_ctx_var
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Get sandbox info to get host_ip and port
            status = await sandbox_manager.get_status(sandbox_id, use_rocklet=False)
            if not status.is_alive or not status.host_ip:
                await asyncio.sleep(2)
                continue
                
            # Try to connect to rocklet service
            port = status.port_mapping.get(Port.PROXY)
            if not port:
                await asyncio.sleep(2)
                continue
                
            # Test if rocklet service is responding
            try:
                await HttpUtils.get(
                    url=f"http://{status.host_ip}:{port}/",
                    headers={
                        "sandbox_id": sandbox_id,
                        EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get(),
                    },
                    read_timeout=5,
                )
                logger.info(f"Rocklet service is ready for sandbox {sandbox_id}")
                return
            except Exception:
                # Service not ready yet, continue waiting
                await asyncio.sleep(2)
                continue
        except Exception as e:
            logger.debug(f"Waiting for rocklet service: {e}")
            await asyncio.sleep(2)
            
    raise Exception(f"Rocklet service not ready within {timeout}s for sandbox {sandbox_id}")


async def _test_get_status_with_redis(sandbox_manager: SandboxManager, use_rocklet: bool):
    """Helper function to test get_status with Redis"""
    from rock.admin.core.redis_key import alive_sandbox_key
    
    # Submit a sandbox
    response = await sandbox_manager.submit(DockerDeploymentConfig(image="python:3.11"))
    sandbox_id = response.sandbox_id
    
    try:
        # Wait for sandbox to be alive
        await check_sandbox_status_until_alive(sandbox_manager, sandbox_id)
        
        # If using rocklet, wait for rocklet HTTP service to be ready
        # if use_rocklet:
        #     await wait_for_rocklet_service_ready(sandbox_manager, sandbox_id)
        
        # Test: get_status with Redis
        status_response = await sandbox_manager.get_status(sandbox_id, use_rocklet=use_rocklet)
        
        # Common assertions
        assert status_response.sandbox_id == sandbox_id
        assert status_response.host_ip is not None
        assert status_response.host_name is not None
        assert status_response.is_alive is True
        assert status_response.state == State.RUNNING
        assert len(status_response.port_mapping) > 0
        assert status_response.image == "python:3.11"
        
        # Verify Redis was used/updated
        redis_data = await sandbox_manager._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        assert redis_data is not None
        assert len(redis_data) > 0
        
        # Additional assertions for rocklet mode
        if use_rocklet:
            # Verify remote status was fetched (phases should be populated)
            assert status_response.status is not None
            assert "docker_run" in status_response.status
    finally:
        # Cleanup
        await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status_with_redis_without_rocklet(sandbox_manager: SandboxManager):
    """Test get_status: with Redis, without rocklet (use_rocklet=False)"""
    await _test_get_status_with_redis(sandbox_manager, use_rocklet=False)


@pytest.mark.skip(reason="Skip this test after rocklet port is fixed")
@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status_with_redis_with_rocklet(sandbox_manager: SandboxManager):
    """Test get_status: with Redis, with rocklet (use_rocklet=True)"""
    await _test_get_status_with_redis(sandbox_manager, use_rocklet=True)

async def _test_get_status_without_redis(rock_config: RockConfig, ray_service, use_rocklet: bool):
    """Helper function to test get_status without Redis"""
    # Create sandbox_manager without Redis
    sandbox_manager_no_redis = SandboxManager(
        rock_config,
        redis_provider=None,  # No Redis
        ray_namespace=rock_config.ray.namespace,
        ray_service=ray_service,
        enable_runtime_auto_clear=False,
    )
    
    # Submit a sandbox
    response = await sandbox_manager_no_redis.submit(DockerDeploymentConfig(image="python:3.11"))
    sandbox_id = response.sandbox_id
    
    try:
        # Wait for sandbox to be alive
        await check_sandbox_status_until_alive(sandbox_manager_no_redis, sandbox_id)
        
        # If using rocklet, wait for rocklet HTTP service to be ready
        if use_rocklet:
            await wait_for_rocklet_service_ready(sandbox_manager_no_redis, sandbox_id)
        
        # Test: get_status without Redis
        status_response = await sandbox_manager_no_redis.get_status(sandbox_id, use_rocklet=use_rocklet)
        
        # Common assertions
        assert status_response.sandbox_id == sandbox_id
        assert status_response.host_ip is not None
        assert status_response.host_name is not None
        assert status_response.is_alive is True
        assert status_response.state == State.RUNNING
        assert len(status_response.port_mapping) > 0
        assert status_response.image == "python:3.11"
        assert status_response.status is not None
        
        # Additional assertions for rocklet mode
        if use_rocklet:
            # Verify remote status was fetched (phases should be populated)
            assert "docker_run" in status_response.status
    finally:
        # Cleanup
        await sandbox_manager_no_redis.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status_without_redis_without_rocklet(rock_config: RockConfig, ray_init_shutdown, ray_service):
    """Test get_status: without Redis, without rocklet (use_rocklet=False)"""
    await _test_get_status_without_redis(rock_config, ray_service, use_rocklet=False)


@pytest.mark.skip(reason="Skip this test after rocklet port is fixed")
@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_status_without_redis_with_rocklet(rock_config: RockConfig, ray_init_shutdown, ray_service):
    """Test get_status: without Redis, with rocklet (use_rocklet=True)"""
    await _test_get_status_without_redis(rock_config, ray_service, use_rocklet=True)
