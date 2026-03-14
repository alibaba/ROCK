import datetime
from unittest.mock import MagicMock, patch

import pytest
import ray

from rock.deployments.config import LocalDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.base_actor import BaseActor
from rock.sandbox.sandbox_actor import SandboxActor
from rock.utils.system import get_host_name

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


class ConcreteBaseActor(BaseActor):
    """Minimal concrete subclass used only for unit testing BaseActor."""

    async def get_sandbox_statistics(self):
        return {"cpu": 10.0, "mem": 20.0, "disk": 30.0, "net": 40.0}


def _make_actor() -> ConcreteBaseActor:
    """Create a ConcreteBaseActor with lightweight mocked dependencies."""
    config = MagicMock()
    config.container_name = "test-container"
    config.auto_clear_time = None  # skip DockerDeploymentConfig branch

    deployment = MagicMock()
    deployment.__class__ = object  # make isinstance(deployment, DockerDeployment) return False

    actor = ConcreteBaseActor(config, deployment)
    actor.host = "127.0.0.1"
    # Pre-populate all gauges with mocks so tests can override selectively
    for key in ("cpu", "mem", "disk", "net", "rt"):
        actor._gauges[key] = MagicMock()
    return actor


@pytest.mark.asyncio
async def test_life_span_rt_gauge_is_set_during_metrics_collection():
    """life_span_rt gauge must be set with the elapsed timedelta after collection."""
    actor = _make_actor()
    mock_rt_gauge = MagicMock()
    actor._gauges["rt"] = mock_rt_gauge

    await actor._collect_sandbox_metrics("test-container")

    assert mock_rt_gauge.set.called, "life_span_rt gauge.set() was never called"
    life_span_rt_value = mock_rt_gauge.set.call_args[0][0]
    assert isinstance(life_span_rt_value, float), f"Expected float, got {type(life_span_rt_value)}"
    assert life_span_rt_value >= 0, "life_span_rt must be non-negative"


@pytest.mark.asyncio
async def test_life_span_rt_increases_over_time():
    """life_span_rt reported on a second call must be >= the first call's value."""
    actor = _make_actor()
    mock_rt_gauge = MagicMock()
    actor._gauges["rt"] = mock_rt_gauge

    await actor._collect_sandbox_metrics("test-container")
    first_rt: datetime.timedelta = mock_rt_gauge.set.call_args[0][0]

    await actor._collect_sandbox_metrics("test-container")
    second_rt: datetime.timedelta = mock_rt_gauge.set.call_args[0][0]

    assert second_rt >= first_rt, f"life_span_rt should be non-decreasing: first={first_rt}, second={second_rt}"


@pytest.mark.asyncio
async def test_life_span_rt_attributes_contain_expected_keys():
    """Attributes passed to life_span_rt gauge must include all standard dimension keys."""
    actor = _make_actor()
    actor._env = "prod"
    actor._role = "worker"
    actor._user_id = "user-42"
    actor._experiment_id = "exp-7"
    actor._namespace = "ns-test"
    actor.host = "10.0.0.1"

    mock_rt_gauge = MagicMock()
    actor._gauges["rt"] = mock_rt_gauge

    await actor._collect_sandbox_metrics("test-container")

    attributes = mock_rt_gauge.set.call_args[1]["attributes"]
    expected_keys = {"sandbox_id", "env", "role", "host", "ip", "user_id", "experiment_id", "namespace"}
    assert expected_keys.issubset(attributes.keys()), f"Missing attribute keys: {expected_keys - attributes.keys()}"
    assert attributes["env"] == "prod"
    assert attributes["role"] == "worker"
    assert attributes["user_id"] == "user-42"
    assert attributes["experiment_id"] == "exp-7"
    assert attributes["namespace"] == "ns-test"


@pytest.mark.asyncio
async def test_life_span_rt_set_even_when_no_cpu_metrics():
    """life_span_rt must be reported even when get_sandbox_statistics returns no cpu data."""

    class NoCpuActor(BaseActor):
        async def get_sandbox_statistics(self):
            return {}  # cpu key absent

    config = MagicMock()
    config.container_name = "no-cpu-container"
    config.auto_clear_time = None
    deployment = MagicMock()
    deployment.__class__ = object

    actor = NoCpuActor(config, deployment)
    actor.host = "127.0.0.1"
    for key in ("cpu", "mem", "disk", "net", "rt"):
        actor._gauges[key] = MagicMock()

    mock_rt_gauge = actor._gauges["rt"]

    await actor._collect_sandbox_metrics("no-cpu-container")

    assert mock_rt_gauge.set.called, "life_span_rt gauge.set() must be called even when cpu metrics are absent"


def test_get_host_name_returns_hostname():
    """get_host_name should return the system hostname under normal conditions."""
    hostname = get_host_name()
    assert isinstance(hostname, str)
    assert len(hostname) > 0


def test_get_host_name_returns_fallback_on_error():
    """get_host_name should return 'unknown_host' when socket.gethostname raises."""
    with patch("rock.utils.system.socket.gethostname", side_effect=OSError("mocked error")):
        result = get_host_name()
        assert result == "unknown_host"


def test_base_actor_host_name_initialized():
    """BaseActor._host_name should be set to the system hostname after __init__."""
    actor = _make_actor()
    assert actor._host_name is not None
    assert isinstance(actor._host_name, str)
    assert len(actor._host_name) > 0


def test_base_actor_host_name_fallback_on_error():
    """BaseActor._host_name should be 'unknown_host' when get_host_name fails."""
    with patch("rock.sandbox.base_actor.get_host_name", return_value="unknown_host"):
        actor = _make_actor()
        assert actor._host_name == "unknown_host"


@pytest.mark.asyncio
async def test_metrics_attributes_contain_host_name():
    """Attributes reported by _collect_sandbox_metrics must include 'host_name'."""
    actor = _make_actor()
    actor._host_name = "my-test-host"

    mock_rt_gauge = MagicMock()
    actor._gauges["rt"] = mock_rt_gauge

    await actor._collect_sandbox_metrics("test-container")

    attributes = mock_rt_gauge.set.call_args[1]["attributes"]
    assert "host_name" in attributes, f"'host_name' missing from attributes: {attributes.keys()}"
    assert attributes["host_name"] == "my-test-host"


@pytest.mark.asyncio
async def test_metrics_attributes_host_name_matches_actor_field():
    """The host_name in metrics attributes should always match actor._host_name."""
    actor = _make_actor()
    custom_hostname = "custom-sandbox-host-42"
    actor._host_name = custom_hostname

    mock_cpu_gauge = MagicMock()
    actor._gauges["cpu"] = mock_cpu_gauge

    await actor._collect_sandbox_metrics("test-container")

    attributes = mock_cpu_gauge.set.call_args[1]["attributes"]
    assert attributes["host_name"] == custom_hostname
