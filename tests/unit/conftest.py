import asyncio
import time
from pathlib import Path

import pytest
import ray
from fakeredis import aioredis
from ray.util.state import list_actors

from rock.admin.core.ray_service import RayService
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.ray import RayOperator
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)


@pytest.fixture(scope="session", autouse=True)
def rock_config():
    config_path = Path(__file__).parent.parent.parent / "rock-conf" / "rock-test.yml"
    return RockConfig.from_env(config_path=config_path)


@pytest.fixture
def docker_deployment_config():
    image = "python:3.11"
    return DockerDeploymentConfig(image=image)


@pytest.fixture
async def redis_provider():
    provider = RedisProvider(host=None, port=None, password="")
    provider.client = aioredis.FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


@pytest.fixture
def ray_service(rock_config: RockConfig, ray_init_shutdown):
    ray_service = RayService(rock_config.ray)
    return ray_service


@pytest.fixture
def ray_operator(ray_service):
    ray_operator = RayOperator(ray_service)
    return ray_operator


@pytest.fixture
async def sandbox_manager(
    rock_config: RockConfig, redis_provider: RedisProvider, ray_init_shutdown, ray_service, ray_operator
):
    sandbox_manager = SandboxManager(
        rock_config,
        redis_provider=redis_provider,
        ray_namespace=rock_config.ray.namespace,
        ray_service=ray_service,
        enable_runtime_auto_clear=rock_config.runtime.enable_auto_clear,
        operator=ray_operator,
    )
    return sandbox_manager


@pytest.fixture
async def sandbox_proxy_service(rock_config: RockConfig, redis_provider: RedisProvider):
    sandbox_proxy_service = SandboxProxyService(rock_config, redis_provider=redis_provider)
    return sandbox_proxy_service


@pytest.fixture(scope="session")
def ray_init_shutdown(rock_config: RockConfig):
    ray_config = rock_config.ray
    ray_namespace = ray_config.namespace

    # if not ray is initialized
    if not ray.is_initialized():
        ray.init(
            address=ray_config.address,
            namespace=ray_namespace,
            runtime_env=ray_config.runtime_env,
            resources=ray_config.resources,
        )
    yield

    try:
        actors = list_actors(filters=[("state", "=", "ALIVE"), ("namespace", "=", ray_namespace)])
        for actor in actors:
            try:
                actor_handle = ray.get_actor(actor["name"], namespace=ray_namespace)
                ray.kill(actor_handle)
                logger.warning(f"Killed actor: {actor['name']}")
            except Exception as e:
                logger.warning(f"Failed to kill actor {actor['name']}: {e}")

        # Wait for cleanup to complete
        time.sleep(2)
    except Exception as e:
        logger.warning(f"Failed to list or clean up actors: {e}")
    finally:
        try:
            ray.shutdown()
        except Exception as e:
            logger.warning(f"Failed to shutdown Ray: {e}")


async def check_sandbox_status_until_alive(sandbox_manager: SandboxManager, sandbox_id: str, timeout: int = 60) -> bool:
    cnt = 0
    while True:
        sandbox_status = await sandbox_manager.get_status(sandbox_id)
        if sandbox_status.is_alive:
            return True
        await asyncio.sleep(1)
        cnt += 1
        if cnt > timeout:
            raise Exception("sandbox not alive")
