import time

import pytest

from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager


@pytest.fixture
async def sandbox_manager(rock_config: RockConfig):
    sandbox_manager = SandboxManager(
        rock_config,
        redis_provider=None,
        ray_namespace=rock_config.ray.namespace,
        enable_runtime_auto_clear=rock_config.runtime.enable_auto_clear,
    )
    return sandbox_manager


@pytest.mark.asyncio
async def test_async_sandbox_start(sandbox_manager: SandboxManager):
    response = await sandbox_manager.start_async(DockerDeploymentConfig())
    sandbox_id = response.sandbox_id
    assert sandbox_id is not None
    search_start_time = time.time()
    while time.time() - search_start_time < 60:
        is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
        if is_alive_response:
            break

    is_alive_response = await sandbox_manager._is_actor_alive(sandbox_id)
    assert is_alive_response

    sandbox_actor = await sandbox_manager.async_ray_get_actor(sandbox_id)
    assert sandbox_actor is not None
    assert await sandbox_actor.user_id.remote() == "default"
    assert await sandbox_actor.experiment_id.remote() == "default"

    await sandbox_manager.stop(sandbox_id)
