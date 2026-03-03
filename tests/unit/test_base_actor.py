import pytest
import ray

from rock.deployments.config import LocalDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor

logger = init_logger(__name__)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_set_and_get_metrics_endpoint(ray_init_shutdown):
    """Test setting and getting metrics endpoint together using Ray actor"""
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "test-set-and-get-metrics-endpoint"

    # Create SandboxActor using Ray
    sandbox_actor = SandboxActor.options(name=actor_name, lifetime="detached").remote(
        sandbox_config, sandbox_config.get_deployment()
    )

    try:
        # Test initial setting
        test_endpoint = "http://test-host:9090/v1/metrics"
        ray.get(sandbox_actor.set_metrics_endpoint.remote(test_endpoint))
        result = ray.get(sandbox_actor.get_metrics_endpoint.remote())
        assert result == test_endpoint
        logger.info(f"Initial endpoint set successfully: {result}")

        # Test updating the endpoint
        new_endpoint = "http://new-host:5000/v1/metrics"
        ray.get(sandbox_actor.set_metrics_endpoint.remote(new_endpoint))
        result = ray.get(sandbox_actor.get_metrics_endpoint.remote())
        assert result == new_endpoint
        logger.info(f"Updated endpoint successfully: {result}")
    finally:
        ray.kill(sandbox_actor)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_metrics_endpoint_default(ray_init_shutdown):
    """Test getting metrics endpoint with default empty value using Ray actor"""
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "test-get-metrics-endpoint-default"

    # Create SandboxActor using Ray
    sandbox_actor = SandboxActor.options(name=actor_name, lifetime="detached").remote(
        sandbox_config, sandbox_config.get_deployment()
    )

    try:
        # Get the default endpoint (should be empty string)
        result = ray.get(sandbox_actor.get_metrics_endpoint.remote())
        assert result == ""
        logger.info(f"Default metrics endpoint is empty as expected: '{result}'")
    finally:
        ray.kill(sandbox_actor)
