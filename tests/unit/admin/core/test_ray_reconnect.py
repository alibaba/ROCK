from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ray_service import RayService


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
