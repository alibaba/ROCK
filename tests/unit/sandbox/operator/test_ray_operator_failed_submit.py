from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ray_service import RayService
from rock.config import RayConfig, RuntimeConfig
from rock.sandbox.operator.ray import RayOperator


def _make_operator() -> tuple[RayOperator, RayService]:
    ray_service = RayService(RayConfig(ray_reconnect_enabled=False))
    with patch("rock.sandbox.operator.ray.ray.is_initialized", return_value=False):
        operator = RayOperator(ray_service=ray_service, runtime_config=RuntimeConfig())
    return operator, ray_service


def _make_actor():
    actor = MagicMock()
    actor.sandbox_info.remote.return_value = object()
    return actor


@pytest.mark.asyncio
async def test_sandbox_info_failure_force_kills_created_actor():
    operator, ray_service = _make_operator()
    actor = _make_actor()
    operator.create_actor = AsyncMock(return_value=actor)
    ray_service.async_ray_get = AsyncMock(side_effect=Exception("ray get timed out"))
    config = MagicMock(container_name="sb-1")
    config.model_dump.return_value = {"container_name": "sb-1"}

    with patch("rock.sandbox.operator.ray.ray.kill") as mock_kill:
        with pytest.raises(Exception, match="ray get timed out"):
            await operator.submit(config)

    mock_kill.assert_called_once_with(actor, no_restart=True)


@pytest.mark.asyncio
async def test_kill_failure_does_not_replace_submit_error():
    operator, ray_service = _make_operator()
    actor = _make_actor()
    operator.create_actor = AsyncMock(return_value=actor)
    ray_service.async_ray_get = AsyncMock(side_effect=Exception("ray get timed out"))
    config = MagicMock(container_name="sb-1")
    config.model_dump.return_value = {"container_name": "sb-1"}

    with patch("rock.sandbox.operator.ray.ray.kill", side_effect=RuntimeError("ray kill failed")):
        with pytest.raises(Exception, match="ray get timed out"):
            await operator.submit(config)


@pytest.mark.asyncio
async def test_non_timeout_sandbox_info_failure_also_kills_actor():
    operator, ray_service = _make_operator()
    actor = _make_actor()
    operator.create_actor = AsyncMock(return_value=actor)
    ray_service.async_ray_get = AsyncMock(side_effect=RuntimeError("ray get failed"))
    config = MagicMock(container_name="sb-1")
    config.model_dump.return_value = {"container_name": "sb-1"}

    with patch("rock.sandbox.operator.ray.ray.kill") as mock_kill:
        with pytest.raises(RuntimeError, match="ray get failed"):
            await operator.submit(config)

    mock_kill.assert_called_once_with(actor, no_restart=True)
