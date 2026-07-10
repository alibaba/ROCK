"""Unit tests for OpenSandboxClient with an injected fake SDK."""

from types import SimpleNamespace

import pytest

from rock.config import OpenSandboxConfig
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sdk.common.exceptions import InternalServerRockError


class FakeSandbox:
    """Stand-in for opensandbox.Sandbox; records interactions on the class."""

    created_kwargs = None
    raise_on_create = False

    def __init__(self, sandbox_id, state="Running"):
        self.id = sandbox_id
        self._state = state

    @classmethod
    async def create(cls, image, **kwargs):
        if cls.raise_on_create:
            raise RuntimeError("boom")
        cls.created_kwargs = {"image": image, **kwargs}
        return cls("osb-new")


class FakeConnectionConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeLifecycleService:
    def __init__(self):
        self.info_ids = []
        self.actions = []

    async def get_sandbox_info(self, sandbox_id):
        self.info_ids.append(sandbox_id)
        return SimpleNamespace(status=SimpleNamespace(state="Running"))

    async def pause_sandbox(self, sandbox_id):
        self.actions.append(("pause", sandbox_id))

    async def resume_sandbox(self, sandbox_id):
        self.actions.append(("resume", sandbox_id))

    async def kill_sandbox(self, sandbox_id):
        self.actions.append(("kill", sandbox_id))


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSandbox.created_kwargs = None
    FakeSandbox.raise_on_create = False


@pytest.fixture
def client():
    result = OpenSandboxClient(
        OpenSandboxConfig(endpoint="opensandbox.local", api_key="k", protocol="http"),
        sandbox_cls=FakeSandbox,
        connection_config_cls=FakeConnectionConfig,
    )
    result._lifecycle_service = FakeLifecycleService()
    return result


@pytest.mark.asyncio
async def test_create_returns_id_and_maps_resources(client):
    osb_id = await client.create(image="python:3.11", cpu="2", memory="8Gi", metadata={"a": "b"})
    assert osb_id == "osb-new"
    assert FakeSandbox.created_kwargs["image"] == "python:3.11"
    assert FakeSandbox.created_kwargs["resource"] == {"cpu": "2", "memory": "8Gi"}
    assert FakeSandbox.created_kwargs["metadata"] == {"a": "b"}
    # create() must not block on readiness — Rock polls get_status for RUNNING.
    assert FakeSandbox.created_kwargs["skip_health_check"] is True


@pytest.mark.asyncio
async def test_create_omits_timeout_when_unset(client):
    # Passing timeout=None explicitly would send a null duration that strict
    # servers reject; when unset we must not pass the kwarg at all.
    await client.create(image="python:3.11", cpu="1", memory="1Gi")
    assert "timeout" not in FakeSandbox.created_kwargs


@pytest.mark.asyncio
async def test_create_passes_timeout_when_set(client):
    from datetime import timedelta

    await client.create(image="python:3.11", cpu="1", memory="1Gi", timeout=300)
    assert FakeSandbox.created_kwargs["timeout"] == timedelta(seconds=300)


@pytest.mark.asyncio
async def test_create_translates_errors(client):
    FakeSandbox.raise_on_create = True
    with pytest.raises(InternalServerRockError, match="opensandbox create failed"):
        await client.create(image="x", cpu="1", memory="1Gi")


@pytest.mark.asyncio
async def test_get_state_reads_lifecycle_service_without_resolving_endpoint(client):
    state = await client.get_state("osb-1")
    assert state == "Running"
    assert client._lifecycle_service.info_ids == ["osb-1"]


@pytest.mark.asyncio
async def test_get_state_returns_none_on_error(client):
    class Boom(FakeLifecycleService):
        async def get_sandbox_info(self, sandbox_id):
            raise RuntimeError("gone")

    client._lifecycle_service = Boom()
    assert await client.get_state("osb-1") is None


@pytest.mark.asyncio
async def test_pause_resume_kill(client):
    await client.pause("osb-1")
    await client.kill("osb-2")
    await client.resume("osb-3")
    assert ("pause", "osb-1") in client._lifecycle_service.actions
    assert ("kill", "osb-2") in client._lifecycle_service.actions
    assert ("resume", "osb-3") in client._lifecycle_service.actions


@pytest.mark.asyncio
async def test_connection_config_built_from_rock_config(client):
    client._connection_config()
    conn = client._conn
    assert conn.kwargs["api_key"] == "k"
    assert conn.kwargs["domain"] == "opensandbox.local"
    assert conn.kwargs["protocol"] == "http"
    assert conn.kwargs["use_server_proxy"] is False
