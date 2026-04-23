import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import ray
from fakeredis import aioredis
from kubernetes import client
from ray.util.state import list_actors

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.ray_service import RayService
from rock.admin.core.sandbox_table import SandboxTable
from rock.config import DatabaseConfig, K8sConfig, RockConfig
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.k8s.api_client import K8sApiClient
from rock.sandbox.operator.k8s.operator import K8sOperator
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader
from rock.sandbox.operator.ray import RayOperator
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
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
def runtime_config(rock_config: RockConfig):
    return rock_config.runtime


@pytest.fixture
def ray_operator(ray_service, runtime_config):
    ray_operator = RayOperator(ray_service, runtime_config)
    ray_operator.set_nacos_provider(None)
    return ray_operator


@pytest.fixture
async def db_provider():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite+aiosqlite:///:memory:"))
    await provider.init()
    await provider.create_tables()
    yield provider
    await provider.close()


@pytest.fixture
async def _memory_sandbox_table(db_provider):
    return SandboxTable(db_provider)


@pytest.fixture
async def sandbox_manager(
    rock_config: RockConfig,
    redis_provider: RedisProvider,
    ray_init_shutdown,
    ray_service,
    ray_operator,
    _memory_sandbox_table: SandboxTable,
):
    meta_store = SandboxMetaStore(redis_provider=redis_provider, sandbox_table=_memory_sandbox_table)
    sandbox_manager = SandboxManager(
        rock_config,
        meta_store=meta_store,
        ray_namespace=rock_config.ray.namespace,
        ray_service=ray_service,
        enable_runtime_auto_clear=rock_config.runtime.enable_auto_clear,
        operator=ray_operator,
    )
    return sandbox_manager


@pytest.fixture
async def sandbox_proxy_service(
    rock_config: RockConfig, redis_provider: RedisProvider, _memory_sandbox_table: SandboxTable
):
    meta_store = SandboxMetaStore(redis_provider=redis_provider, sandbox_table=_memory_sandbox_table)
    sandbox_proxy_service = SandboxProxyService(rock_config, meta_store=meta_store)
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
            _temp_dir=ray_config.temp_dir,
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
    """Create K8sConfig with required templates (pools removed, now from Nacos)."""
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
        # pools removed - now loaded from Nacos
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
        resync_period=60,
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


# ---------------------------------------------------------------------------
# Docker container fixtures - shared across all unit test subdirectories
# (lazy-import docker so non-Docker tests don't require the package)
# ---------------------------------------------------------------------------

_PG_IMAGE = "postgres:16-alpine"
_PG_USER = "rock_test"
_PG_PASSWORD = "rock_test_pass"
_PG_DB = "rock_test_db"
_PG_PORT = 5432
_REDIS_IMAGE = "redis/redis-stack-server:latest"
_REDIS_PORT = 6379


def _docker_keep_containers() -> bool:
    import os

    return os.getenv("ROCK_TEST_KEEP_DOCKER_CONTAINERS", "").lower() in {"1", "true", "yes", "on"}


def _docker_detect_network(client) -> str | None:
    import socket

    import docker

    hostname = socket.gethostname()
    try:
        current = client.containers.get(hostname)
        networks = current.attrs["NetworkSettings"]["Networks"]
        if "bridge" in networks:
            return "bridge"
        return next(iter(networks), None)
    except (docker.errors.NotFound, docker.errors.APIError):
        return None


def _docker_resolve_host_port(container, network_name: str | None, internal_port: int) -> tuple[str, int]:
    container.reload()
    if network_name:
        host = container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        return host, internal_port
    host = "127.0.0.1"
    port = int(container.ports[f"{internal_port}/tcp"][0]["HostPort"])
    return host, port


def _docker_start_container(client, image, name, network_name, internal_port, **extra_kwargs):
    keep = _docker_keep_containers()
    run_kwargs = {"image": image, "name": name, "detach": True, "remove": not keep, **extra_kwargs}
    if network_name:
        run_kwargs["network"] = network_name
    else:
        run_kwargs["ports"] = {f"{internal_port}/tcp": None}
    return client.containers.run(**run_kwargs)


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL 16 Docker container for the test session."""
    import uuid

    import docker

    client = docker.from_env()
    container_name = f"rock-test-pg-{uuid.uuid4().hex[:8]}"
    network_name = _docker_detect_network(client)
    container = _docker_start_container(
        client,
        image=_PG_IMAGE,
        name=container_name,
        network_name=network_name,
        internal_port=_PG_PORT,
        environment={
            "POSTGRES_USER": _PG_USER,
            "POSTGRES_PASSWORD": _PG_PASSWORD,
            "POSTGRES_DB": _PG_DB,
        },
    )
    try:
        # wait for readiness
        import time as _t

        deadline = _t.time() + 30
        while _t.time() < deadline:
            code, _ = container.exec_run(f"pg_isready -U {_PG_USER}")
            if code == 0:
                break
            _t.sleep(0.5)
        else:
            raise TimeoutError("PostgreSQL container did not become ready within 30s")

        # Confirm with a real SQL query to close the startup race window.
        while _t.time() < deadline:
            code, _ = container.exec_run(f'psql -U {_PG_USER} -d {_PG_DB} -c "SELECT 1"')
            if code == 0:
                break
            _t.sleep(0.5)
        else:
            raise TimeoutError("PostgreSQL: pg_isready OK but SELECT 1 still failing")

        host, port = _docker_resolve_host_port(container, network_name, _PG_PORT)
        yield {
            "host": host,
            "port": port,
            "user": _PG_USER,
            "password": _PG_PASSWORD,
            "database": _PG_DB,
            "url": f"postgresql://{_PG_USER}:{_PG_PASSWORD}@{host}:{port}/{_PG_DB}",
        }
    finally:
        if not _docker_keep_containers():
            try:
                container.stop(timeout=5)
            except Exception:
                pass


@pytest.fixture(scope="session")
def redis_container():
    """Start a Redis Stack Docker container (with RedisJSON) for the test session."""
    import uuid

    import docker

    client = docker.from_env()
    container_name = f"rock-test-redis-{uuid.uuid4().hex[:8]}"
    network_name = _docker_detect_network(client)
    container = _docker_start_container(
        client,
        image=_REDIS_IMAGE,
        name=container_name,
        network_name=network_name,
        internal_port=_REDIS_PORT,
    )
    try:
        import time as _t

        deadline = _t.time() + 30
        while _t.time() < deadline:
            code, output = container.exec_run("redis-cli ping")
            if code == 0 and b"PONG" in output:
                break
            _t.sleep(0.5)
        else:
            raise TimeoutError("Redis container did not become ready within 30s")

        host, port = _docker_resolve_host_port(container, network_name, _REDIS_PORT)
        yield {"host": host, "port": port, "password": "", "url": f"redis://{host}:{port}"}
    finally:
        if not _docker_keep_containers():
            try:
                container.stop(timeout=5)
            except Exception:
                pass
