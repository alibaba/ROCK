"""Unit tests for OpenSandboxOperator (lifecycle seam, 方案 B Phase 1)."""

from unittest.mock import AsyncMock

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import StopReason
from rock.config import OpenSandboxConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.opensandbox.operator import (
    OpenSandboxOperator,
    _docker_mem_to_k8s,
    _map_state,
)
from rock.sandbox.operator.opensandbox.session_registry import OpenSandboxSessionRegistry
from rock.sdk.common.exceptions import BadRequestRockError


class FakeClient:
    """Records calls and returns canned values, standing in for OpenSandboxClient."""

    def __init__(self, *, created_id="osb-123", state="Running"):
        self.created_id = created_id
        self.state = state
        self.calls = []

    async def create(self, *, image, cpu, memory, env=None, metadata=None, timeout=None):
        self.calls.append(("create", dict(image=image, cpu=cpu, memory=memory, metadata=metadata)))
        return self.created_id

    async def get_state(self, opensandbox_id):
        self.calls.append(("get_state", opensandbox_id))
        return self.state

    async def pause(self, opensandbox_id):
        self.calls.append(("pause", opensandbox_id))

    async def resume(self, opensandbox_id):
        self.calls.append(("resume", opensandbox_id))

    async def kill(self, opensandbox_id):
        self.calls.append(("kill", opensandbox_id))


@pytest.fixture
def os_config():
    return OpenSandboxConfig(endpoint="opensandbox.local", api_key="k")


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def operator(os_config, client):
    return OpenSandboxOperator(os_config=os_config, client=client)


def _deployment_config(sandbox_id="sbx-1", *, memory="8g", cpus=2, **kwargs):
    return DockerDeploymentConfig(container_name=sandbox_id, image="python:3.11", cpus=cpus, memory=memory, **kwargs)


# ---- helpers ---------------------------------------------------------------


def test_supports_running_delete(operator):
    assert operator.supports_running_delete is True


def test_docker_mem_to_k8s():
    assert _docker_mem_to_k8s("8g") == "8Gi"
    assert _docker_mem_to_k8s("8G") == "8Gi"
    assert _docker_mem_to_k8s("4096m") == "4096Mi"
    assert _docker_mem_to_k8s("512Mi") == "512Mi"  # already k8s style, passthrough
    assert _docker_mem_to_k8s("2Gi") == "2Gi"


def test_map_state():
    assert _map_state("Pending") == State.PENDING
    assert _map_state("Running") == State.RUNNING
    assert _map_state("Paused") == State.STOPPED
    assert _map_state("Pausing") == State.STOPPED
    assert _map_state("Stopping") == State.STOPPED
    assert _map_state("Terminated") == State.DELETED
    assert _map_state("Failed") == State.STOPPED
    assert _map_state("Unknown") == State.PENDING


# ---- submit ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_returns_sandbox_info(operator, client):
    config = _deployment_config(sandbox_id="sbx-1")
    info = await operator.submit(config, user_info={"user_id": "u1", "experiment_id": "e1", "namespace": "n1"})

    assert info["sandbox_id"] == "sbx-1"
    assert info["image"] == "python:3.11"
    assert info["state"] == State.PENDING
    assert info["user_id"] == "u1"
    assert info["experiment_id"] == "e1"
    assert info["namespace"] == "n1"


@pytest.mark.asyncio
async def test_submit_maps_resources_and_metadata(operator, client):
    config = _deployment_config(sandbox_id="sbx-2", memory="8g", cpus=4)
    await operator.submit(config, user_info={"user_id": "u1"})

    call = dict(client.calls[0][1])
    assert call["memory"] == "8Gi"
    assert call["cpu"] == "4"
    assert call["metadata"]["rock_sandbox_id"] == "sbx-2"
    assert call["metadata"]["user_id"] == "u1"


@pytest.mark.asyncio
async def test_submit_records_backend_and_opensandbox_id(operator, client):
    client.created_id = "osb-xyz"
    config = _deployment_config(sandbox_id="sbx-3")
    info = await operator.submit(config, user_info={})

    assert info["extended_params"]["backend"] == "opensandbox"
    assert info["extended_params"]["opensandbox_id"] == "osb-xyz"


@pytest.mark.asyncio
async def test_submit_leaves_image_unchanged_without_registry_prefix(operator, client):
    config = _deployment_config()
    config.image = " python:3.11 "

    info = await operator.submit(config, user_info={})

    call = dict(client.calls[0][1])
    assert call["image"] == " python:3.11 "
    assert info["image"] == " python:3.11 "


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("python:3.11", "registry.example.com/library/python:3.11"),
        ("team/python:3.11", "registry.example.com/library/team/python:3.11"),
        ("ghcr.io/team/python:3.11", "ghcr.io/team/python:3.11"),
        ("localhost:5000/python:3.11", "localhost:5000/python:3.11"),
    ],
)
async def test_submit_applies_configured_image_registry_prefix(client, image, expected):
    operator = OpenSandboxOperator(
        OpenSandboxConfig(image_registry_prefix="registry.example.com/library"),
        client=client,
    )
    config = _deployment_config()
    config.image = image

    info = await operator.submit(config, user_info={})

    call = dict(client.calls[0][1])
    assert call["image"] == expected
    assert info["image"] == expected


# ---- get_status ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_maps_state(operator, client):
    client.state = "Running"
    operator._redis_provider = AsyncMock()
    operator._redis_provider.json_get = AsyncMock(
        return_value=[{"sandbox_id": "sbx-1", "extended_params": {"opensandbox_id": "osb-1"}}]
    )
    info = await operator.get_status("sbx-1")
    assert info["state"] == State.RUNNING
    assert ("get_state", "osb-1") in client.calls


@pytest.mark.asyncio
async def test_get_status_returns_none_when_not_in_redis(operator):
    operator._redis_provider = AsyncMock()
    operator._redis_provider.json_get = AsyncMock(return_value=None)
    assert await operator.get_status("missing") is None


@pytest.mark.asyncio
async def test_get_status_returns_none_when_opensandbox_gone(operator, client):
    client.state = None  # not found on OpenSandbox side
    operator._redis_provider = AsyncMock()
    operator._redis_provider.json_get = AsyncMock(
        return_value=[{"sandbox_id": "sbx-1", "extended_params": {"opensandbox_id": "osb-1"}}]
    )
    assert await operator.get_status("sbx-1") is None


# ---- stop / restart / delete ----------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_unsupported_by_default(operator, client):
    operator._redis_provider = AsyncMock()
    operator._redis_provider.json_get = AsyncMock(return_value=[{"extended_params": {"opensandbox_id": "osb-1"}}])
    with pytest.raises(BadRequestRockError, match="does not support stop"):
        await operator.stop("sbx-1", reason=StopReason.MANUAL)
    assert ("pause", "osb-1") not in client.calls


@pytest.mark.asyncio
async def test_restart_is_unsupported_by_default(operator, client):
    config = _deployment_config(sandbox_id="sbx-1", extended_params={"opensandbox_id": "osb-1"})
    with pytest.raises(BadRequestRockError, match="does not support restart"):
        await operator.restart(config)
    assert ("resume", "osb-1") not in client.calls


@pytest.mark.asyncio
async def test_delete_kills(operator, client):
    config = _deployment_config(sandbox_id="sbx-1", extended_params={"opensandbox_id": "osb-1"})
    ok = await operator.delete(config)
    assert ok is True
    assert ("kill", "osb-1") in client.calls


@pytest.mark.asyncio
async def test_delete_clears_persisted_session_mappings(os_config, client, redis_provider):
    registry = OpenSandboxSessionRegistry(redis_provider)
    reservation = await registry.reserve("sbx-1", "worker")
    assert reservation is not None
    assert await registry.commit("sbx-1", "worker", reservation, "os-session-1") is True
    operator = OpenSandboxOperator(os_config=os_config, redis_provider=redis_provider, client=client)

    await operator.delete(_deployment_config(sandbox_id="sbx-1", extended_params={"opensandbox_id": "osb-1"}))

    assert await registry.get("sbx-1", "worker") is None


@pytest.mark.asyncio
async def test_delete_succeeds_when_session_mapping_cleanup_fails(os_config, client, redis_provider, monkeypatch):
    clear = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(OpenSandboxSessionRegistry, "clear", clear)
    operator = OpenSandboxOperator(os_config=os_config, redis_provider=redis_provider, client=client)

    ok = await operator.delete(_deployment_config(sandbox_id="sbx-1", extended_params={"opensandbox_id": "osb-1"}))

    assert ok is True
    assert ("kill", "osb-1") in client.calls
    clear.assert_awaited_once_with("sbx-1")
