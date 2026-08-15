from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions import CommandResponse
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
async def test_delete_uses_worker_rocklet_without_creating_actor():
    operator, ray_service = _make_operator()
    operator.create_actor = AsyncMock()
    ray_service.async_ray_get_actor = AsyncMock()
    ray_service.get_ray_rwlock = MagicMock(side_effect=AssertionError("delete must not acquire the Ray lock"))
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=CommandResponse(stdout="sb-1\n", stderr="", exit_code=0))
    config = DockerDeploymentConfig(
        container_name="sb-1",
        cpus=4,
        memory="8g",
        disk="128g",
    )

    with (
        patch("rock.sandbox.operator.ray.RemoteSandboxRuntime", return_value=runtime) as runtime_cls,
        patch("rock.sandbox.operator.ray.ray.kill") as kill,
    ):
        result = await operator.delete(config, host_ip="10.0.0.1")

    assert result is True
    assert config.cpus == 4
    assert config.memory == "8g"
    assert config.disk == "128g"
    operator.create_actor.assert_not_awaited()
    ray_service.get_ray_rwlock.assert_not_called()
    ray_service.async_ray_get_actor.assert_not_awaited()
    kill.assert_not_called()
    runtime_cls.assert_called_once_with(host="10.0.0.1", port=22555)
    runtime.execute.assert_awaited_once()
    command = runtime.execute.await_args.args[0]
    assert command.command == "docker rm -f -v sb-1"
    assert command.timeout == 10
    assert command.shell is True
    assert command.check is False
    assert command.sandbox_id == "sb-1"


@pytest.mark.asyncio
async def test_delete_rocklet_failure_propagates():
    operator, _ = _make_operator()
    runtime = MagicMock()
    runtime.execute = AsyncMock(side_effect=Exception("rocklet timed out"))
    config = DockerDeploymentConfig(container_name="sb-1", disk="128g")

    with (
        patch("rock.sandbox.operator.ray.RemoteSandboxRuntime", return_value=runtime),
        pytest.raises(Exception, match="rocklet timed out"),
    ):
        await operator.delete(config, host_ip="10.0.0.1")

    runtime.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_kata_disk_through_worker_rocklet():
    operator, _ = _make_operator()
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=CommandResponse(stdout="sb-1\n", stderr="", exit_code=0))
    config = DockerDeploymentConfig(
        container_name="sb-1",
        use_kata_runtime=True,
        kata_disk_base_path="/data/docker-disk",
    )

    with patch("rock.sandbox.operator.ray.RemoteSandboxRuntime", return_value=runtime):
        await operator.delete(config, host_ip="10.0.0.1")

    runtime.execute.assert_awaited_once()
    command = runtime.execute.await_args.args[0]
    assert command.command == (
        "docker rm -f -v sb-1; docker_status=$?; "
        "rm -f -- /data/docker-disk/sb-1.img; kata_status=$?; "
        '[ "$docker_status" -eq 0 ] && [ "$kata_status" -eq 0 ]'
    )
    assert command.timeout == 10
    assert command.shell is True


@pytest.mark.asyncio
async def test_delete_requires_worker_host_ip():
    operator, _ = _make_operator()

    with pytest.raises(ValueError, match="requires the worker host_ip"):
        await operator.delete(DockerDeploymentConfig(container_name="sb-1"))


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
