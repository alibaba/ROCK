import asyncio
import time
from pathlib import Path

import pytest
import ray
from fakeredis import aioredis
from ray.util.state import list_actors

from unittest.mock import MagicMock

from kubernetes import client

from rock.admin.core.ray_service import RayService
from rock.config import K8sConfig, RockConfig
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.k8s.api_client import K8sApiClient
from rock.sandbox.operator.k8s.operator import K8sOperator
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader
from rock.sandbox.operator.ray import RayOperator
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)


class MockDeploymentConfig(DeploymentConfig):
    """Mock deployment config for testing."""

    image: str = "python:3.11"
    cpus: float = 2
    memory: str = "4Gi"
    container_name: str | None = None
    template_name: str = "default"
    auto_clear_time_minutes: int = 30

    def get_deployment(self) -> AbstractDeployment:
        """Mock implementation."""
        return MagicMock()


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
    ray_operator.set_nacos_provider(None)
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


# ========== K8S Fixtures ==========


@pytest.fixture
def k8s_config():
    """Create K8sConfig with required templates."""
    return K8sConfig(
        kubeconfig_path=None,
        templates={
            "default": {
                "namespace": "rock-test",
                "ports": {
                    "proxy": 8000,
                    "server": 8080,
                    "ssh": 22,
                },
                "template": {
                    "metadata": {"labels": {"app": "test"}},
                    "spec": {"containers": [{"name": "main", "image": "python:3.11"}]},
                },
            }
        },
    )


@pytest.fixture
def basic_templates():
    """Create basic template configuration."""
    return {
        "default": {
            "ports": {
                "proxy": 8000,
                "server": 8080,
                "ssh": 22,
            },
            "template": {
                "metadata": {"labels": {"app": "rock-sandbox"}},
                "spec": {"containers": [{"name": "main", "image": "python:3.11"}]},
            },
        }
    }


@pytest.fixture
def template_loader(basic_templates):
    """Create template loader instance."""
    return K8sTemplateLoader(templates=basic_templates, default_namespace="rock-test")


@pytest.fixture
def mock_api_client():
    """Create mock K8S ApiClient."""
    return MagicMock(spec=client.ApiClient)


@pytest.fixture
def k8s_api_client(mock_api_client):
    """Create K8sApiClient instance."""
    return K8sApiClient(
        api_client=mock_api_client,
        group="sandbox.opensandbox.io",
        version="v1alpha1",
        plural="batchsandboxes",
        namespace="rock-test",
        qps=5.0,
        watch_timeout_seconds=60,
        watch_reconnect_delay_seconds=5,
    )


@pytest.fixture
def mock_provider():
    """Create mock BatchSandboxProvider."""
    from unittest.mock import AsyncMock

    return AsyncMock()


@pytest.fixture
def k8s_operator(k8s_config, mock_provider):
    """Create K8sOperator instance with mock provider."""
    from unittest.mock import patch

    with patch("rock.sandbox.operator.k8s.operator.BatchSandboxProvider", return_value=mock_provider):
        operator = K8sOperator(k8s_config=k8s_config)
        operator._provider = mock_provider
        return operator


@pytest.fixture
def deployment_config():
    """Create deployment configuration."""
    return MockDeploymentConfig(
        image="python:3.11",
        cpus=2,
        memory="4Gi",
        container_name="test-sandbox",
        template_name="default",
    )
