"""Unit tests for OpenSandboxClient with an injected fake SDK."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rock.config import OpenSandboxConfig
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sdk.common.exceptions import InternalServerRockError


class FakeSandbox:
    """Stand-in for opensandbox.Sandbox; records interactions on the class."""

    created_kwargs = None
    raise_on_create = False
    connected_kwargs = None
    connected_handle = None

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
        cls.connected_kwargs = {"sandbox_id": sandbox_id, **kwargs}
        return cls.connected_handle


class FakeConnectionConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeRunCommandOpts:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeLifecycleService:
    def __init__(self):
        self.info_ids = []
        self.endpoint_requests = []
        self.actions = []

    async def get_sandbox_info(self, sandbox_id):
        self.info_ids.append(sandbox_id)
        return SimpleNamespace(status=SimpleNamespace(state="Running"))

    async def get_sandbox_endpoint(self, sandbox_id, port, use_server_proxy):
        self.endpoint_requests.append((sandbox_id, port, use_server_proxy))
        return SimpleNamespace(
            endpoint=f"https://proxy.example/sandboxes/{sandbox_id}/ports/{port}",
            headers={"X-Sandbox-Token": "required"},
        )

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
    FakeSandbox.connected_kwargs = None
    FakeSandbox.connected_handle = None


@pytest.fixture
def client():
    result = OpenSandboxClient(
        OpenSandboxConfig(endpoint="opensandbox.local", api_key="k", protocol="http"),
        sandbox_cls=FakeSandbox,
        connection_config_cls=FakeConnectionConfig,
        run_command_opts_cls=FakeRunCommandOpts,
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
async def test_get_endpoint_uses_lifecycle_service_and_proxy_setting(client):
    client._config.use_server_proxy = True

    endpoint = await client.get_endpoint("osb-1", 8080)

    assert endpoint.endpoint == "https://proxy.example/sandboxes/osb-1/ports/8080"
    assert endpoint.headers == {"X-Sandbox-Token": "required"}
    assert client._lifecycle_service.endpoint_requests == [("osb-1", 8080, True)]


@pytest.mark.asyncio
async def test_get_endpoint_translates_errors(client):
    client._lifecycle_service.get_sandbox_endpoint = AsyncMock(side_effect=RuntimeError("endpoint unavailable"))

    with pytest.raises(InternalServerRockError, match="opensandbox get_endpoint failed for osb-1"):
        await client.get_endpoint("osb-1", 9000)


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
    assert conn.kwargs["request_timeout"] == timedelta(seconds=600)


class FakeRuntimeHandle:
    def __init__(self):
        self.commands = SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(exit_code=0)),
            create_session=AsyncMock(return_value="session-1"),
            run_in_session=AsyncMock(return_value=SimpleNamespace(exit_code=0)),
            delete_session=AsyncMock(),
        )
        self.files = SimpleNamespace(
            read_bytes=AsyncMock(return_value=b"hello"),
            get_file_info=AsyncMock(return_value={}),
            write_file=AsyncMock(),
        )
        self.close = AsyncMock()


@pytest.fixture
def runtime_client():
    transport = SimpleNamespace(aclose=AsyncMock())
    result = OpenSandboxClient(
        OpenSandboxConfig(endpoint="opensandbox.local", default_timeout=45),
        sandbox_cls=FakeSandbox,
        connection_config_cls=FakeConnectionConfig,
        run_command_opts_cls=FakeRunCommandOpts,
        transport=transport,
    )
    result._lifecycle_service = FakeLifecycleService()
    return result, transport


@pytest.mark.asyncio
async def test_read_bytes_connects_without_health_check_and_closes_handle(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    FakeSandbox.connected_handle = handle

    result = await client.read_bytes("osb-1", "/tmp/a")

    assert result == b"hello"
    assert FakeSandbox.connected_kwargs["skip_health_check"] is True
    handle.files.read_bytes.assert_awaited_once_with("/tmp/a")
    handle.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_handle_closes_when_operation_fails(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    handle.files.read_bytes.side_effect = RuntimeError("boom")
    FakeSandbox.connected_handle = handle

    with pytest.raises(InternalServerRockError, match="opensandbox read_bytes failed"):
        await client.read_bytes("osb-1", "/tmp/a")

    handle.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_file_info_returns_empty_mapping_when_path_is_missing(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    missing = RuntimeError("file not found")
    missing.error = SimpleNamespace(code="FILE_NOT_FOUND")
    handle.files.get_file_info.side_effect = missing
    FakeSandbox.connected_handle = handle

    assert await client.get_file_info("osb-1", "/tmp/missing") == {}
    handle.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_primitives_delegate_to_handle(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    FakeSandbox.connected_handle = handle
    stream = SimpleNamespace()

    await client.execute("osb-1", "pwd", timeout=3, cwd="/tmp", env={"A": "B"})
    await client.get_file_info("osb-1", "/tmp/a")
    await client.write_file("osb-1", "/tmp/a", stream, mode=644)

    run_opts = handle.commands.run.await_args.kwargs["opts"]
    assert run_opts.timeout == timedelta(seconds=3)
    assert run_opts.working_directory == "/tmp"
    assert run_opts.envs == {"A": "B"}
    handle.files.get_file_info.assert_awaited_once_with(["/tmp/a"])
    handle.files.write_file.assert_awaited_once_with("/tmp/a", stream, mode=644)
    assert handle.close.await_count == 3


@pytest.mark.asyncio
async def test_session_primitives_delegate_to_handle(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    FakeSandbox.connected_handle = handle

    session_id = await client.create_session("osb-1", cwd="/workspace")
    execution = await client.run_in_session(
        "osb-1",
        session_id,
        "pwd",
        timeout=3,
        cwd="/tmp",
    )
    await client.delete_session("osb-1", session_id)

    assert session_id == "session-1"
    assert execution.exit_code == 0
    handle.commands.create_session.assert_awaited_once_with(working_directory="/workspace")
    handle.commands.run_in_session.assert_awaited_once_with(
        "session-1",
        "pwd",
        timeout=timedelta(seconds=3),
        working_directory="/tmp",
    )
    handle.commands.delete_session.assert_awaited_once_with("session-1")
    assert handle.close.await_count == 3


@pytest.mark.asyncio
async def test_run_in_session_preserves_unset_timeout_and_cwd(runtime_client):
    client, _ = runtime_client
    handle = FakeRuntimeHandle()
    FakeSandbox.connected_handle = handle

    await client.run_in_session("osb-1", "session-1", "echo ok")

    handle.commands.run_in_session.assert_awaited_once_with(
        "session-1",
        "echo ok",
        timeout=None,
        working_directory=None,
    )


@pytest.mark.asyncio
async def test_aclose_closes_injected_transport_once(runtime_client):
    client, transport = runtime_client

    await client.aclose()
    await client.aclose()

    transport.aclose.assert_awaited_once()


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_default_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="default_timeout must be positive"):
        OpenSandboxClient(OpenSandboxConfig(default_timeout=timeout))
