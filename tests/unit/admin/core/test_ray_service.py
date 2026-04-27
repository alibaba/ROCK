from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ray_service import RayService
from rock.deployments.config import RayDeploymentConfig
from rock.deployments.ray import RayDeployment
from rock.sandbox.sandbox_actor import SandboxActor


def _mock_write_lock(service):
    """Helper to set up a mock write lock on a RayService instance."""
    mock_lock_cm = AsyncMock()
    mock_lock = MagicMock()
    mock_lock.__aenter__ = mock_lock_cm.__aenter__
    mock_lock.__aexit__ = mock_lock_cm.__aexit__
    mock_rwlock = MagicMock()
    mock_rwlock.write_lock.return_value = mock_lock
    service._ray_rwlock = mock_rwlock
    return mock_rwlock, mock_lock


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_reconnect_ray_calls_ray_shutdown_and_init_and_reset_counters(ray_service: RayService):
    service = ray_service

    service._ray_request_count = 123
    old_establish_time = service._ray_establish_time

    mock_rwlock, mock_lock = _mock_write_lock(service)

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init") as mock_init,
        patch("rock.admin.core.ray_service.ray.cluster_resources") as mock_cluster,
        patch("time.time", return_value=old_establish_time + 5),
    ):
        await service._reconnect_ray()

        mock_rwlock.write_lock.assert_called_once()
        mock_lock.__aenter__.assert_awaited()
        mock_lock.__aexit__.assert_awaited()

        mock_shutdown.assert_called_once()
        mock_init.assert_called_once()
        mock_cluster.assert_called_once()

        assert service._ray_request_count == 0

        assert service._ray_establish_time == old_establish_time + 5

        assert service._ray_establish_time != old_establish_time


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_reconnect_ray_skip_when_reader_exists_and_write_lock_timeout(ray_service: RayService):
    service = ray_service

    service._ray_request_count = 123
    service._config.ray_reconnect_wait_timeout_seconds = 5

    old_count = service._ray_request_count
    old_est = service._ray_establish_time

    service._ray_rwlock._readers = 1

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init") as mock_init,
        patch("time.time", return_value=old_est + 5),
    ):
        await service._reconnect_ray()

        mock_shutdown.assert_not_called()
        mock_init.assert_not_called()

        assert service._ray_request_count == old_count
        assert service._ray_establish_time == old_est


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_reconnect_ray_catches_init_exception_and_triggers_retry(ray_service: RayService):
    """When ray.init() raises a non-InternalServerRockError exception,
    _reconnect_ray should catch it and call _retry_ray_init."""
    service = ray_service
    service._ray_request_count = 50
    old_count = service._ray_request_count
    old_est = service._ray_establish_time

    _mock_write_lock(service)

    with (
        patch("rock.admin.core.ray_service.ray.shutdown"),
        patch("rock.admin.core.ray_service.ray.init", side_effect=ConnectionError("head node unreachable")),
        patch("rock.admin.core.ray_service.ray.cluster_resources"),
        patch.object(service, "_retry_ray_init", new_callable=AsyncMock) as mock_retry,
    ):
        await service._reconnect_ray()

        mock_retry.assert_awaited_once()
        assert service._ray_request_count == old_count
        assert service._ray_establish_time == old_est


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_retry_ray_init_succeeds(ray_service: RayService):
    """_retry_ray_init should succeed on a single retry attempt."""
    service = ray_service
    service._ray_request_count = 99

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init"),
        patch("rock.admin.core.ray_service.ray.cluster_resources"),
    ):
        await service._retry_ray_init()

        assert service._ray_request_count == 0
        mock_shutdown.assert_called_once()


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_retry_ray_init_fails(ray_service: RayService):
    """When the single recovery attempt fails, counters should NOT be reset."""
    service = ray_service
    service._ray_request_count = 99
    old_count = service._ray_request_count
    old_est = service._ray_establish_time

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init", side_effect=ConnectionError("head node unreachable")),
        patch("rock.admin.core.ray_service.ray.cluster_resources"),
    ):
        await service._retry_ray_init()

        assert service._ray_request_count == old_count
        assert service._ray_establish_time == old_est
        mock_shutdown.assert_called_once()


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_reconnect_ray_timeout_on_hanging_init(ray_service: RayService):
    """When _do_ray_init hangs, the timeout should fire and trigger _retry_ray_init."""

    service = ray_service
    service._config.ray_init_timeout_seconds = 0.1

    _mock_write_lock(service)

    def blocking_init(*args, **kwargs):
        import time

        time.sleep(5)

    with (
        patch("rock.admin.core.ray_service.ray.shutdown"),
        patch("rock.admin.core.ray_service.ray.init", side_effect=blocking_init),
        patch("rock.admin.core.ray_service.ray.cluster_resources"),
        patch.object(service, "_retry_ray_init", new_callable=AsyncMock) as mock_retry,
    ):
        await service._reconnect_ray()

        mock_retry.assert_awaited_once()


@pytest.mark.need_docker
@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_ray_get(ray_service):
    import uuid

    import ray

    service = ray_service
    # Unique name to avoid colliding with leaked detached actors from prior runs/reruns
    actor_name = f"test-ray-get-{uuid.uuid4().hex[:8]}"
    namespace = "rock-sandbox-test"
    config = RayDeploymentConfig(image="python:3.11")
    deployment: RayDeployment = RayDeployment.from_config(config)

    actor = SandboxActor.options(name=actor_name, namespace=namespace, lifetime="detached").remote(config, deployment)
    try:
        await service.async_ray_get(actor.start.remote())
        result = await service.async_ray_get(actor.host_name.remote())
        assert result is not None

        fetched_actor = await service.async_ray_get_actor(actor_name, namespace)
        assert fetched_actor is not None

        await service.async_ray_get(fetched_actor.stop.remote())
    finally:
        # Ensure detached actor is always killed, even if assertions/RPCs above fail
        try:
            leaked = ray.get_actor(actor_name, namespace=namespace)
            ray.kill(leaked)
        except Exception:
            pass
