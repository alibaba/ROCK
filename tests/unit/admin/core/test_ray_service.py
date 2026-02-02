from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ray_service import RayService
from rock.deployments.config import RayDeploymentConfig
from rock.deployments.ray import RayDeployment
from rock.sandbox.sandbox_actor import SandboxActor


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

    with patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown, patch(
        "rock.admin.core.ray_service.ray.init"
    ) as mock_init, patch("time.time", return_value=old_establish_time + 5):
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

    with patch("rock.admin.core.ray_service.ray.shutdown") as mock_shutdown, patch(
        "rock.admin.core.ray_service.ray.init"
    ) as mock_init, patch("time.time", return_value=old_est + 5):
        await service._reconnect_ray()

        mock_shutdown.assert_not_called()
        mock_init.assert_not_called()

        assert service._ray_request_count == old_count
        assert service._ray_establish_time == old_est


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_ray_get(ray_service):
    service = ray_service
    config = RayDeploymentConfig(image="python:3.11")
    deployment: RayDeployment = RayDeployment.from_config(config)
    actor = SandboxActor.options(**{"name": "test", "lifetime": "detached"}).remote(config, deployment)
    await service.async_ray_get(actor.start.remote())
    result = await service.async_ray_get(actor.host_name.remote())
    assert result is not None
    actor = await service.async_ray_get_actor("test", "rock-sandbox-test")
    assert actor is not None
    await service.async_ray_get(actor.stop.remote())
