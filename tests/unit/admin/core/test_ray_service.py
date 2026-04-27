from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import InternalServerRockError
from rock.admin.core.ray_service import RayService
from rock.config import RayConfig
from rock.deployments.config import RayDeploymentConfig
from rock.deployments.ray import RayDeployment
from rock.sandbox.sandbox_actor import SandboxActor


def _make_service(**overrides) -> RayService:
    cfg_kwargs = dict(
        address=None,
        ray_reconnect_enabled=False,
        ray_reconnect_wait_timeout_seconds=1,
        ray_reconnect_max_attempts=3,
        ray_reconnect_retry_backoff_seconds=0,
    )
    cfg_kwargs.update(overrides)
    return RayService(RayConfig(**cfg_kwargs))


@pytest.mark.asyncio
async def test_reconnect_ray_retries_on_init_failure_and_eventually_succeeds():
    service = _make_service(ray_reconnect_max_attempts=3)
    service._ray_request_count = 99
    old_establish_time = service._ray_establish_time

    init_calls = {"n": 0}

    def init_side_effect(**_kwargs):
        init_calls["n"] += 1
        if init_calls["n"] < 3:
            raise ConnectionError("ray head unreachable")

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init", side_effect=init_side_effect) as mock_init,
        patch("rock.admin.core.ray_service.time.time", return_value=old_establish_time + 5),
    ):
        await service._reconnect_ray()

    assert mock_init.call_count == 3
    assert mock_shutdown.call_count == 3
    assert service._ray_request_count == 0
    assert service._ray_establish_time == old_establish_time + 5


@pytest.mark.asyncio
async def test_reconnect_ray_does_not_reset_counters_when_all_attempts_fail():
    service = _make_service(ray_reconnect_max_attempts=2)
    service._ray_request_count = 99
    old_establish_time = service._ray_establish_time

    with (
        patch("rock.admin.core.ray_service.ray.shutdown"),
        patch("rock.admin.core.ray_service.ray.init", side_effect=ConnectionError("down")),
    ):
        await service._reconnect_ray()

    # Counters preserved so the next scheduler tick will retry promptly.
    assert service._ray_request_count == 99
    assert service._ray_establish_time == old_establish_time


@pytest.mark.asyncio
async def test_reconnect_ray_releases_write_lock_after_init_failure():
    service = _make_service(ray_reconnect_max_attempts=1)

    with (
        patch("rock.admin.core.ray_service.ray.shutdown"),
        patch("rock.admin.core.ray_service.ray.init", side_effect=ConnectionError("down")),
    ):
        await service._reconnect_ray()

    # After the failed reconnect a reader must still be able to acquire the lock.
    async with service._ray_rwlock.read_lock():
        pass


@pytest.mark.asyncio
async def test_async_ray_get_raises_when_ray_not_initialized():
    service = _make_service()

    fake_ref = MagicMock()
    with (
        patch("rock.admin.core.ray_service.ray.is_initialized", return_value=False),
        patch("rock.admin.core.ray_service.ray.get") as mock_ray_get,
    ):
        with pytest.raises(InternalServerRockError):
            await service.async_ray_get(fake_ref, timeout=1)

    # Must short-circuit BEFORE touching ray.get; otherwise auto-init could spawn a
    # local Ray cluster on the admin host.
    mock_ray_get.assert_not_called()


@pytest.mark.asyncio
async def test_async_ray_get_actor_raises_when_ray_not_initialized():
    service = _make_service()

    with (
        patch("rock.admin.core.ray_service.ray.is_initialized", return_value=False),
        patch("rock.admin.core.ray_service.ray.get_actor") as mock_get_actor,
    ):
        with pytest.raises(InternalServerRockError):
            await service.async_ray_get_actor("any-actor", namespace="ns")

    mock_get_actor.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_ray_logs_critical_when_all_attempts_exhausted():
    service = _make_service(ray_reconnect_max_attempts=2)

    with (
        patch("rock.admin.core.ray_service.ray.shutdown"),
        patch("rock.admin.core.ray_service.ray.init", side_effect=ConnectionError("down")),
        patch("rock.admin.core.ray_service.logger") as mock_logger,
    ):
        await service._reconnect_ray()

    assert mock_logger.critical.called, "expected logger.critical when ray reconnect exhausts all retries"


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_reconnect_ray_calls_ray_shutdown_and_init_and_reset_counters(ray_service: RayService):
    service = ray_service

    service._ray_request_count = 123
    old_establish_time = service._ray_establish_time

    mock_lock_cm = AsyncMock()
    mock_lock = MagicMock()
    mock_lock.__aenter__ = mock_lock_cm.__aenter__
    mock_lock.__aexit__ = mock_lock_cm.__aexit__

    mock_rwlock = MagicMock()
    mock_rwlock.write_lock.return_value = mock_lock
    service._ray_rwlock = mock_rwlock

    with (
        patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown,
        patch("rock.admin.core.ray_service.ray.init") as mock_init,
        patch("time.time", return_value=old_establish_time + 5),
    ):
        await service._reconnect_ray()

        mock_rwlock.write_lock.assert_called_once()
        mock_lock.__aenter__.assert_awaited()
        mock_lock.__aexit__.assert_awaited()

        mock_shutdown.assert_called_once()
        mock_init.assert_called_once_with(
            address=ray_service._config.address,
            runtime_env=ray_service._config.runtime_env,
            namespace=ray_service._config.namespace,
            resources=ray_service._config.resources,
            _temp_dir=ray_service._config.temp_dir,
        )

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
