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


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_set_and_get_user_defined_tags(ray_init_shutdown):
    """Test setting and getting user_defined_tags using Ray actor"""
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "test-set-and-get-user-defined-tags"

    # Create SandboxActor using Ray
    sandbox_actor = SandboxActor.options(name=actor_name, lifetime="detached").remote(
        sandbox_config, sandbox_config.get_deployment()
    )

    try:
        # Test setting custom tags
        custom_tags = {
            "service": "rock-sandbox",
            "version": "1.0.0",
        }
        ray.get(sandbox_actor.set_user_defined_tags.remote(custom_tags))
        result = ray.get(sandbox_actor.get_user_defined_tags.remote())

        # Verify all custom tags are set correctly
        assert result == custom_tags
        assert result["service"] == "rock-sandbox"
        assert result["version"] == "1.0.0"
        logger.info(f"Custom tags set successfully: {result}")
    finally:
        ray.kill(sandbox_actor)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_user_defined_tags_default(ray_init_shutdown):
    """Test getting user_defined_tags with default empty dict using Ray actor"""
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "test-get-user-defined-tags-default"

    # Create SandboxActor using Ray
    sandbox_actor = SandboxActor.options(name=actor_name, lifetime="detached").remote(
        sandbox_config, sandbox_config.get_deployment()
    )

    try:
        # Get the default user_defined_tags (should be empty dict)
        result = ray.get(sandbox_actor.get_user_defined_tags.remote())
        assert result == {}
        assert isinstance(result, dict)
        logger.info(f"Default user_defined_tags is empty dict as expected: {result}")
    finally:
        ray.kill(sandbox_actor)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_user_defined_tags_with_empty_dict(ray_init_shutdown):
    """Test setting user_defined_tags with empty dict using Ray actor"""
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "test-user-defined-tags-empty-dict"

    # Create SandboxActor using Ray
    sandbox_actor = SandboxActor.options(name=actor_name, lifetime="detached").remote(
        sandbox_config, sandbox_config.get_deployment()
    )

    try:
        # Set empty dict
        ray.get(sandbox_actor.set_user_defined_tags.remote({}))
        result = ray.get(sandbox_actor.get_user_defined_tags.remote())
        assert result == {}
        logger.info(f"Empty dict set successfully: {result}")
    finally:
        ray.kill(sandbox_actor)
