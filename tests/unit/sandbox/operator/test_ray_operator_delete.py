from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ray_service import RayService
from rock.config import RayConfig, RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


def _make_operator() -> tuple[RayOperator, RayService]:
    ray_service = RayService(RayConfig(ray_reconnect_enabled=False))
    with patch("rock.sandbox.operator.ray.ray.is_initialized", return_value=False):
        operator = RayOperator(ray_service=ray_service, runtime_config=RuntimeConfig())
    return operator, ray_service


@pytest.mark.asyncio
async def test_delete_actor_drops_sandbox_resources():
    operator, ray_service = _make_operator()
    actor = MagicMock()
    delete_ref = object()
    actor.delete.remote.return_value = delete_ref
    operator.create_actor = AsyncMock(return_value=actor)
    ray_service.async_ray_get_actor = AsyncMock(side_effect=ValueError("actor not found"))
    ray_service.async_ray_get = AsyncMock(return_value=None)
    config = DockerDeploymentConfig(
        container_name="sb-1",
        cpus=4,
        memory="8g",
        disk="128g",
    )

    with patch("rock.sandbox.operator.ray.ray.kill") as kill:
        result = await operator.delete(config, host_ip="10.0.0.1")

    assert result is True
    assert config.cpus == 0
    assert config.memory == "0"
    assert config.disk is None
    operator._disk_scheduling_enabled = True
    actor_options = operator._generate_actor_options(config, pin_to_host_ip="10.0.0.1")
    assert actor_options["num_cpus"] == 0
    assert actor_options["memory"] == 0
    assert actor_options["resources"] == {"node:10.0.0.1": 0.001}
    operator.create_actor.assert_awaited_once_with(config, pin_to_host_ip="10.0.0.1")
    ray_service.async_ray_get.assert_awaited_once_with(delete_ref)
    kill.assert_called_once_with(actor)


@pytest.mark.asyncio
async def test_delete_actor_timeout_propagates_and_kills_pending_actor():
    operator, ray_service = _make_operator()
    actor = MagicMock()
    actor.delete.remote.return_value = object()
    operator.create_actor = AsyncMock(return_value=actor)
    ray_service.async_ray_get_actor = AsyncMock(side_effect=ValueError("actor not found"))
    ray_service.async_ray_get = AsyncMock(side_effect=Exception("ray get timed out"))
    config = DockerDeploymentConfig(container_name="sb-1", disk="128g")

    with (
        patch("rock.sandbox.operator.ray.ray.kill") as kill,
        pytest.raises(Exception, match="ray get timed out"),
    ):
        await operator.delete(config, host_ip="10.0.0.1")

    assert config.disk is None
    ray_service.async_ray_get.assert_awaited_once()
    assert ray_service.async_ray_get.await_args.kwargs == {}
    kill.assert_called_once_with(actor)


@pytest.mark.asyncio
async def test_archive_actor_drops_sandbox_resources():
    operator, _ = _make_operator()
    actor = MagicMock()
    operator.create_actor = AsyncMock(return_value=actor)
    config = DockerDeploymentConfig(
        container_name="sb-1",
        cpus=4,
        memory="8g",
        disk="128g",
    )
    dir_storage_config = {"bucket": "logs"}
    image_storage_config = {"registry_url": "registry.example.com"}
    archive_params = {"timeout_seconds": 3600}

    await operator.start_archive(
        config,
        host_ip="10.0.0.1",
        dir_storage_config=dir_storage_config,
        image_storage_config=image_storage_config,
        archive_params=archive_params,
    )

    assert config.cpus == 0
    assert config.memory == "0"
    assert config.disk is None
    operator._disk_scheduling_enabled = True
    actor_options = operator._generate_actor_options(config, pin_to_host_ip="10.0.0.1")
    assert actor_options["num_cpus"] == 0
    assert actor_options["memory"] == 0
    assert actor_options["resources"] == {"node:10.0.0.1": 0.001}
    operator.create_actor.assert_awaited_once_with(config, pin_to_host_ip="10.0.0.1")
    actor.archive.remote.assert_called_once_with(
        dir_storage_config,
        image_storage_config,
        archive_params,
    )
