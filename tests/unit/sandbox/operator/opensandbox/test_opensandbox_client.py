"""Unit tests for OpenSandboxClient with an injected fake SDK."""

from types import SimpleNamespace

import pytest

from rock.config import OpenSandboxConfig
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sdk.common.exceptions import InternalServerRockError


class FakeSandbox:
    """Stand-in for opensandbox.Sandbox; records interactions on the class."""

    created_kwargs = None
    connected_id = None
    resumed_id = None
    actions = []
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

    @classmethod
    async def connect(cls, sandbox_id, **kwargs):
        cls.connected_id = sandbox_id
        return cls(sandbox_id)

    @classmethod
    async def resume(cls, sandbox_id, **kwargs):
        cls.resumed_id = sandbox_id

    async def get_info(self):
        return SimpleNamespace(status=SimpleNamespace(state=self._state))

    async def pause(self):
        type(self).actions.append(("pause", self.id))

    async def kill(self):
        type(self).actions.append(("kill", self.id))


class FakeConnectionConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSandbox.created_kwargs = None
    FakeSandbox.connected_id = None
    FakeSandbox.resumed_id = None
    FakeSandbox.actions = []
    FakeSandbox.raise_on_create = False


@pytest.fixture
def client():
    return OpenSandboxClient(
        OpenSandboxConfig(endpoint="opensandbox.local", api_key="k", protocol="http"),
        sandbox_cls=FakeSandbox,
        connection_config_cls=FakeConnectionConfig,
    )


@pytest.mark.asyncio
async def test_create_returns_id_and_maps_resources(client):
    osb_id = await client.create(image="python:3.11", cpu="2", memory="8Gi", metadata={"a": "b"})
    assert osb_id == "osb-new"
    assert FakeSandbox.created_kwargs["image"] == "python:3.11"
    assert FakeSandbox.created_kwargs["resource"] == {"cpu": "2", "memory": "8Gi"}
    assert FakeSandbox.created_kwargs["metadata"] == {"a": "b"}


@pytest.mark.asyncio
async def test_create_translates_errors(client):
    FakeSandbox.raise_on_create = True
    with pytest.raises(InternalServerRockError, match="opensandbox create failed"):
        await client.create(image="x", cpu="1", memory="1Gi")


@pytest.mark.asyncio
async def test_get_state_connects_and_reads(client):
    state = await client.get_state("osb-1")
    assert state == "Running"
    assert FakeSandbox.connected_id == "osb-1"


@pytest.mark.asyncio
async def test_get_state_returns_none_on_error(client):
    class Boom(FakeSandbox):
        @classmethod
        async def connect(cls, sandbox_id, **kwargs):
            raise RuntimeError("gone")

    client._sandbox_cls = Boom
    assert await client.get_state("osb-1") is None


@pytest.mark.asyncio
async def test_pause_resume_kill(client):
    await client.pause("osb-1")
    await client.kill("osb-2")
    await client.resume("osb-3")
    assert ("pause", "osb-1") in FakeSandbox.actions
    assert ("kill", "osb-2") in FakeSandbox.actions
    assert FakeSandbox.resumed_id == "osb-3"


@pytest.mark.asyncio
async def test_connection_config_built_from_rock_config(client):
    client._connection_config()
    conn = client._conn
    assert conn.kwargs["api_key"] == "k"
    assert conn.kwargs["domain"] == "opensandbox.local"
    assert conn.kwargs["protocol"] == "http"
    assert conn.kwargs["use_server_proxy"] is True
