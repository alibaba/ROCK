"""Unit tests for CompositeOperator and multi-operator support.

Covers:
- RuntimeConfig operator_types backward compatibility
- CompositeOperator routing logic (submit / get_status / stop)
- CompositeOperator.set_redis_provider propagation to sub-operators
- get_status does NOT overwrite operator_type in Redis (critical)
- OperatorFactory.create_composite_operator
- SandboxStartRequest / DockerDeploymentConfig operator_type field
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import aioredis

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import alive_sandbox_key
from rock.admin.proto.request import SandboxStartRequest
from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.composite import CompositeOperator
from rock.utils.providers.redis_provider import RedisProvider


def _make_mock_operator(operator_name: str = "mock") -> AbstractOperator:
    """Create a mock AbstractOperator with async methods."""
    operator = AsyncMock(spec=AbstractOperator)
    operator._operator_name = operator_name
    operator.set_redis_provider = MagicMock()
    operator.set_nacos_provider = MagicMock()
    return operator


def _make_sandbox_info(**overrides) -> SandboxInfo:
    """Create a minimal SandboxInfo dict for testing."""
    info: SandboxInfo = {
        "sandbox_id": "test-sandbox-001",
        "host_ip": "10.0.0.1",
        "host_name": "test-host",
        "image": "python:3.11",
        "state": State.PENDING,
        "cpus": 2,
        "memory": "8g",
        "phases": {},
        "port_mapping": {},
    }
    info.update(overrides)
    return info


@pytest.fixture
async def fake_redis_provider():
    """Create a RedisProvider backed by fakeredis."""
    provider = RedisProvider(host=None, port=None, password="")
    provider.client = aioredis.FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


def test_runtime_config_default_operator_types_from_operator_type():
    """When operator_types is empty, it should be populated from operator_type."""
    config = RuntimeConfig(operator_type="ray")
    assert config.operator_types == ["ray"]
    assert config.operator_type == "ray"


def test_runtime_config_explicit_operator_types_list():
    """When operator_types is explicitly set, it should be used as-is."""
    config = RuntimeConfig(operator_types=["ray", "k8s"])
    assert config.operator_types == ["ray", "k8s"]
    assert config.operator_type == "ray"


def test_runtime_config_operator_types_overrides_operator_type():
    """operator_types takes precedence; operator_type is synced to the first element."""
    config = RuntimeConfig(operator_type="ray", operator_types=["k8s", "ray"])
    assert config.operator_types == ["k8s", "ray"]
    assert config.operator_type == "k8s"


def test_runtime_config_single_operator_types():
    """Single-element operator_types should work like the old operator_type."""
    config = RuntimeConfig(operator_types=["k8s"])
    assert config.operator_types == ["k8s"]
    assert config.operator_type == "k8s"


def test_composite_init_with_valid_operators():
    """CompositeOperator should initialize correctly with valid operators."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")
    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )
    assert composite._default_operator_type == "ray"
    assert len(composite._operators) == 2


def test_composite_init_with_empty_operators_raises():
    """CompositeOperator should raise ValueError with empty operators dict."""
    with pytest.raises(ValueError, match="At least one operator"):
        CompositeOperator(operators={}, default_operator_type="ray")


def test_composite_init_with_invalid_default_raises():
    """CompositeOperator should raise ValueError when default type is not in operators."""
    ray_op = _make_mock_operator("ray")
    with pytest.raises(ValueError, match="not found in provided operators"):
        CompositeOperator(operators={"ray": ray_op}, default_operator_type="k8s")


def test_composite_init_normalizes_default_type():
    """CompositeOperator should normalize default_operator_type to lowercase."""
    ray_op = _make_mock_operator("ray")
    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="RAY",
    )
    assert composite._default_operator_type == "ray"


def test_set_redis_provider_propagates_to_all():
    """set_redis_provider should propagate to all sub-operators."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")
    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )

    mock_redis = MagicMock(spec=RedisProvider)
    composite.set_redis_provider(mock_redis)

    ray_op.set_redis_provider.assert_called_once_with(mock_redis)
    k8s_op.set_redis_provider.assert_called_once_with(mock_redis)
    assert composite._redis_provider is mock_redis


@pytest.mark.asyncio
async def test_submit_routes_to_specified_operator():
    """When config.operator_type is set, submit should route to that operator."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")

    ray_op.submit.return_value = _make_sandbox_info(sandbox_id="ray-sandbox")
    k8s_op.submit.return_value = _make_sandbox_info(sandbox_id="k8s-sandbox")

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )

    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="test-k8s",
        operator_type="k8s",
    )
    result = await composite.submit(config, {"user_id": "u1"})

    k8s_op.submit.assert_awaited_once_with(config, {"user_id": "u1"})
    ray_op.submit.assert_not_awaited()
    assert result["operator_type"] == "k8s"


@pytest.mark.asyncio
async def test_submit_uses_default_when_no_operator_type():
    """When config.operator_type is None, submit should use the default operator."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")

    ray_op.submit.return_value = _make_sandbox_info(sandbox_id="ray-sandbox")

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )

    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="test-default",
        operator_type=None,
    )
    result = await composite.submit(config, {})

    ray_op.submit.assert_awaited_once()
    k8s_op.submit.assert_not_awaited()
    assert result["operator_type"] == "ray"


@pytest.mark.asyncio
async def test_submit_sets_operator_type_in_sandbox_info():
    """submit() must write operator_type into the returned SandboxInfo."""
    ray_op = _make_mock_operator("ray")
    ray_op.submit.return_value = _make_sandbox_info()

    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="ray",
    )

    config = DockerDeploymentConfig(image="python:3.11", container_name="test")
    result = await composite.submit(config, {})

    assert "operator_type" in result
    assert result["operator_type"] == "ray"


@pytest.mark.asyncio
async def test_submit_with_unsupported_operator_type_raises():
    """submit() should raise ValueError for unsupported operator_type."""
    ray_op = _make_mock_operator("ray")
    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="ray",
    )

    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="test",
        operator_type="docker_swarm",
    )
    with pytest.raises(ValueError, match="Unsupported operator type"):
        await composite.submit(config, {})


@pytest.mark.asyncio
async def test_get_status_routes_by_redis_operator_type(fake_redis_provider):
    """get_status should look up operator_type from Redis and route accordingly."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")

    k8s_status = _make_sandbox_info(sandbox_id="sandbox-1", state=State.RUNNING)
    k8s_op.get_status.return_value = k8s_status

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )
    composite.set_redis_provider(fake_redis_provider)

    sandbox_info_in_redis = _make_sandbox_info(sandbox_id="sandbox-1", operator_type="k8s")
    await fake_redis_provider.json_set(alive_sandbox_key("sandbox-1"), "$", sandbox_info_in_redis)

    await composite.get_status("sandbox-1")

    k8s_op.get_status.assert_awaited_once_with("sandbox-1")
    ray_op.get_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_status_falls_back_to_default_without_redis():
    """Without Redis, get_status should fall back to the default operator."""
    ray_op = _make_mock_operator("ray")
    ray_op.get_status.return_value = _make_sandbox_info(state=State.RUNNING)

    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="ray",
    )

    await composite.get_status("sandbox-no-redis")
    ray_op.get_status.assert_awaited_once_with("sandbox-no-redis")


@pytest.mark.asyncio
async def test_get_status_falls_back_when_redis_has_no_operator_type(fake_redis_provider):
    """If Redis entry has no operator_type, fall back to default."""
    ray_op = _make_mock_operator("ray")
    ray_op.get_status.return_value = _make_sandbox_info(state=State.RUNNING)

    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="ray",
    )
    composite.set_redis_provider(fake_redis_provider)

    sandbox_info_no_type = _make_sandbox_info(sandbox_id="sandbox-2")
    await fake_redis_provider.json_set(alive_sandbox_key("sandbox-2"), "$", sandbox_info_no_type)

    await composite.get_status("sandbox-2")
    ray_op.get_status.assert_awaited_once_with("sandbox-2")


@pytest.mark.asyncio
async def test_stop_routes_by_redis_operator_type(fake_redis_provider):
    """stop should look up operator_type from Redis and route accordingly."""
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")
    k8s_op.stop.return_value = True

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )
    composite.set_redis_provider(fake_redis_provider)

    sandbox_info_in_redis = _make_sandbox_info(sandbox_id="sandbox-stop", operator_type="k8s")
    await fake_redis_provider.json_set(alive_sandbox_key("sandbox-stop"), "$", sandbox_info_in_redis)

    result = await composite.stop("sandbox-stop")

    k8s_op.stop.assert_awaited_once_with("sandbox-stop")
    ray_op.stop.assert_not_awaited()
    assert result is True


@pytest.mark.asyncio
async def test_stop_falls_back_to_default_without_redis():
    """Without Redis, stop should fall back to the default operator."""
    ray_op = _make_mock_operator("ray")
    ray_op.stop.return_value = True

    composite = CompositeOperator(
        operators={"ray": ray_op},
        default_operator_type="ray",
    )

    await composite.stop("sandbox-no-redis")
    ray_op.stop.assert_awaited_once_with("sandbox-no-redis")


@pytest.mark.asyncio
async def test_operator_type_survives_submit_and_get_status_cycle(fake_redis_provider):
    """Critical: full cycle submit -> Redis write -> get_status -> Redis write
    must preserve operator_type in Redis.

    Simulates the SandboxManager flow:
      1. CompositeOperator.submit() sets operator_type in SandboxInfo
      2. SandboxManager.start_async() writes SandboxInfo to Redis
      3. SandboxManager.get_status() calls operator.get_status() and writes back
      4. operator_type must still be present in Redis after step 3
    """
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")

    k8s_op.submit.return_value = _make_sandbox_info(sandbox_id="cycle-test")

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )
    composite.set_redis_provider(fake_redis_provider)

    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="cycle-test",
        operator_type="k8s",
    )
    sandbox_info = await composite.submit(config, {})
    assert sandbox_info["operator_type"] == "k8s"

    await fake_redis_provider.json_set(alive_sandbox_key("cycle-test"), "$", sandbox_info)

    redis_data = await fake_redis_provider.json_get(alive_sandbox_key("cycle-test"), "$")
    assert redis_data[0]["operator_type"] == "k8s"

    k8s_op.get_status.return_value = _make_sandbox_info(
        sandbox_id="cycle-test",
        state=State.RUNNING,
    )
    status_info = await composite.get_status("cycle-test")

    await fake_redis_provider.json_set(alive_sandbox_key("cycle-test"), "$", status_info)

    redis_data_after = await fake_redis_provider.json_get(alive_sandbox_key("cycle-test"), "$")
    has_operator_type = "operator_type" in redis_data_after[0]

    if not has_operator_type:
        pytest.fail(
            "operator_type was lost from Redis after get_status! "
            "The sub-operator's get_status() did not include operator_type, "
            "and SandboxManager overwrote Redis with the incomplete data."
        )


@pytest.mark.asyncio
async def test_ray_operator_get_status_preserves_operator_type_via_redis_merge():
    """Simulate RayOperator.get_status() redis merge path.

    RayOperator.get_status() (non-rocklet path) does:
        redis_info = await self.get_sandbox_info_from_redis(sandbox_id)
        if redis_info:
            redis_info.update(sandbox_info)
            return redis_info

    The merge preserves operator_type because actor_sandbox_info doesn't contain it.
    """
    redis_sandbox_info = _make_sandbox_info(
        sandbox_id="ray-merge-test",
        operator_type="ray",
        user_id="user-1",
        experiment_id="exp-1",
    )

    actor_sandbox_info = _make_sandbox_info(
        sandbox_id="ray-merge-test",
        state=State.RUNNING,
    )
    assert "operator_type" not in actor_sandbox_info

    redis_sandbox_info.update(actor_sandbox_info)

    assert redis_sandbox_info.get("operator_type") == "ray"
    assert redis_sandbox_info.get("state") == State.RUNNING


@pytest.mark.asyncio
async def test_k8s_operator_get_status_preserves_operator_type_via_redis_merge():
    """Simulate K8sOperator.get_status() redis merge path.

    K8sOperator.get_status() does:
        sandbox_info = await self._provider.get_status(sandbox_id)
        if self._redis_provider:
            redis_info = await self._get_sandbox_info_from_redis(sandbox_id)
            if redis_info:
                redis_info.update(sandbox_info)
                return redis_info

    The merge preserves operator_type because provider_sandbox_info doesn't contain it.
    """
    redis_sandbox_info = _make_sandbox_info(
        sandbox_id="k8s-merge-test",
        operator_type="k8s",
        user_id="user-1",
    )

    provider_sandbox_info: SandboxInfo = {
        "sandbox_id": "k8s-merge-test",
        "host_ip": "10.0.0.2",
        "state": State.RUNNING,
        "phases": {},
        "port_mapping": {8000: 30001},
    }
    assert "operator_type" not in provider_sandbox_info

    redis_sandbox_info.update(provider_sandbox_info)

    assert redis_sandbox_info.get("operator_type") == "k8s"
    assert redis_sandbox_info.get("state") == State.RUNNING
    assert redis_sandbox_info.get("host_ip") == "10.0.0.2"


@pytest.mark.asyncio
async def test_sandbox_manager_get_status_preserves_operator_type(fake_redis_provider):
    """End-to-end: SandboxManager.get_status() must preserve operator_type in Redis.

    If the operator returns sandbox_info WITH operator_type (because sub-operators
    merge from Redis), then the json_set will preserve it.
    """
    ray_op = _make_mock_operator("ray")
    k8s_op = _make_mock_operator("k8s")

    composite = CompositeOperator(
        operators={"ray": ray_op, "k8s": k8s_op},
        default_operator_type="ray",
    )
    composite.set_redis_provider(fake_redis_provider)

    initial_info = _make_sandbox_info(
        sandbox_id="e2e-test",
        operator_type="k8s",
        user_id="user-1",
    )
    await fake_redis_provider.json_set(alive_sandbox_key("e2e-test"), "$", initial_info)

    merged_status = _make_sandbox_info(
        sandbox_id="e2e-test",
        operator_type="k8s",
        user_id="user-1",
        state=State.RUNNING,
        host_ip="10.0.0.5",
    )
    k8s_op.get_status.return_value = merged_status

    result = await composite.get_status("e2e-test")

    await fake_redis_provider.json_set(alive_sandbox_key("e2e-test"), "$", result)

    final_redis = await fake_redis_provider.json_get(alive_sandbox_key("e2e-test"), "$")
    assert final_redis[0]["operator_type"] == "k8s"
    assert final_redis[0]["state"] == State.RUNNING


def test_create_composite_operator_single_type():
    """create_composite_operator with a single operator type."""
    from rock.sandbox.operator.factory import OperatorContext, OperatorFactory

    runtime_config = RuntimeConfig(operator_types=["ray"])
    ray_service = MagicMock()

    context = OperatorContext(
        runtime_config=runtime_config,
        ray_service=ray_service,
    )

    with patch("rock.sandbox.operator.factory.OperatorFactory._create_single_operator") as mock_create:
        mock_ray_op = _make_mock_operator("ray")
        mock_create.return_value = mock_ray_op

        composite = OperatorFactory.create_composite_operator(context)

        assert isinstance(composite, CompositeOperator)
        assert composite._default_operator_type == "ray"
        assert "ray" in composite._operators


def test_create_composite_operator_multiple_types():
    """create_composite_operator with multiple operator types."""
    from rock.sandbox.operator.factory import OperatorContext, OperatorFactory

    runtime_config = RuntimeConfig(operator_types=["ray", "k8s"])
    ray_service = MagicMock()

    context = OperatorContext(
        runtime_config=runtime_config,
        ray_service=ray_service,
    )

    call_count = 0

    def side_effect(op_type, ctx):
        nonlocal call_count
        call_count += 1
        return _make_mock_operator(op_type)

    with patch(
        "rock.sandbox.operator.factory.OperatorFactory._create_single_operator",
        side_effect=side_effect,
    ):
        composite = OperatorFactory.create_composite_operator(context)

        assert isinstance(composite, CompositeOperator)
        assert composite._default_operator_type == "ray"
        assert "ray" in composite._operators
        assert "k8s" in composite._operators
        assert call_count == 2


def test_sandbox_start_request_has_operator_type():
    """SandboxStartRequest should accept and store operator_type."""
    request = SandboxStartRequest(
        image="python:3.11",
        operator_type="k8s",
    )
    assert request.operator_type == "k8s"


def test_sandbox_start_request_operator_type_default_none():
    """SandboxStartRequest.operator_type should default to None."""
    request = SandboxStartRequest(image="python:3.11")
    assert request.operator_type is None


def test_docker_deployment_config_has_operator_type():
    """DockerDeploymentConfig should accept and store operator_type."""
    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="test",
        operator_type="ray",
    )
    assert config.operator_type == "ray"


def test_docker_deployment_config_operator_type_default_none():
    """DockerDeploymentConfig.operator_type should default to None."""
    config = DockerDeploymentConfig(
        image="python:3.11",
        container_name="test",
    )
    assert config.operator_type is None


def test_docker_deployment_config_from_request_preserves_operator_type():
    """DockerDeploymentConfig.from_request should carry over operator_type."""
    request = SandboxStartRequest(
        image="python:3.11",
        sandbox_id="test-sandbox",
        operator_type="k8s",
    )
    config = DockerDeploymentConfig.from_request(request)
    assert config.operator_type == "k8s"
    assert config.container_name == "test-sandbox"
