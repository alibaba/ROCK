from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.datastructures import Headers

from rock.actions.sandbox.response import State
from rock.admin.proto.request import (
    SandboxBashAction,
    SandboxCloseBashSessionRequest,
    SandboxCreateBashSessionRequest,
)
from rock.rocklet.exceptions import SessionDoesNotExistError, SessionExistsError
from rock.sandbox.service.opensandbox_proxy_service import OPENSANDBOX_BACKEND, OpenSandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError


def _info():
    return {
        "sandbox_id": "sbx-1",
        "state": State.RUNNING,
        "extended_params": {"backend": OPENSANDBOX_BACKEND, "opensandbox_id": "osb-1"},
    }


@pytest.fixture
def service():
    backend = AsyncMock()
    registry = AsyncMock()
    registry.reserve.return_value = "reservation-1"
    registry.commit.return_value = True
    result = OpenSandboxProxyService.__new__(OpenSandboxProxyService)
    result._opensandbox_backend = backend
    result._session_registry = registry
    result._opensandbox_protocol = "https"
    result._meta_store = SimpleNamespace(get=AsyncMock(return_value=_info()), update=AsyncMock())
    result._update_expire_time = AsyncMock()
    return result, backend


@pytest.mark.asyncio
async def test_create_session_reserves_and_commits_mapping(service):
    proxy, backend = service
    backend.create_session.return_value = ("os-session-1", SimpleNamespace(output="ready", session_type="bash"))
    request = SandboxCreateBashSessionRequest(session="worker", sandbox_id="sbx-1")

    response = await proxy.create_session(request)

    assert response.output == "ready"
    proxy._session_registry.reserve.assert_awaited_once_with("sbx-1", "worker")
    backend.create_session.assert_awaited_once_with("sbx-1", _info(), request)
    proxy._session_registry.commit.assert_awaited_once_with("sbx-1", "worker", "reservation-1", "os-session-1")


@pytest.mark.asyncio
async def test_create_session_rejects_duplicate_before_backend_call(service):
    proxy, backend = service
    proxy._session_registry.reserve.return_value = None

    with pytest.raises(SessionExistsError, match="worker"):
        await proxy.create_session(SandboxCreateBashSessionRequest(session="worker", sandbox_id="sbx-1"))

    backend.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_session_releases_reservation_when_backend_fails(service):
    proxy, backend = service
    backend.create_session.side_effect = RuntimeError("create failed")

    with pytest.raises(RuntimeError, match="create failed"):
        await proxy.create_session(SandboxCreateBashSessionRequest(session="worker", sandbox_id="sbx-1"))

    proxy._session_registry.rollback.assert_awaited_once_with("sbx-1", "worker", "reservation-1")


@pytest.mark.asyncio
async def test_create_session_preserves_commit_error_when_cleanup_also_fails(service):
    proxy, backend = service
    backend.create_session.return_value = ("os-session-1", SimpleNamespace(output="", session_type="bash"))
    proxy._session_registry.commit.side_effect = RuntimeError("commit failed")
    backend.close_session.side_effect = RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        await proxy.create_session(SandboxCreateBashSessionRequest(session="worker", sandbox_id="sbx-1"))

    proxy._session_registry.rollback.assert_awaited_once_with("sbx-1", "worker", "reservation-1")


@pytest.mark.asyncio
async def test_create_session_closes_remote_session_when_reservation_expires(service):
    proxy, backend = service
    backend.create_session.return_value = ("os-session-1", SimpleNamespace(output="", session_type="bash"))
    proxy._session_registry.commit.return_value = False

    with pytest.raises(RuntimeError, match="reservation expired"):
        await proxy.create_session(SandboxCreateBashSessionRequest(session="worker", sandbox_id="sbx-1"))

    backend.close_session.assert_awaited_once_with("sbx-1", _info(), "os-session-1")
    proxy._session_registry.rollback.assert_awaited_once_with("sbx-1", "worker", "reservation-1")


@pytest.mark.asyncio
async def test_run_session_resolves_persisted_mapping(service):
    proxy, backend = service
    proxy._session_registry.get.return_value = "os-session-1"
    backend.run_session.return_value = SimpleNamespace(output="ok", exit_code=0)
    action = SandboxBashAction(command="pwd", session="worker", sandbox_id="sbx-1")

    response = await proxy.run_in_session(action)

    assert response.output == "ok"
    backend.run_session.assert_awaited_once_with("sbx-1", _info(), "os-session-1", action)


@pytest.mark.asyncio
async def test_run_session_preserves_missing_session_error(service):
    proxy, backend = service
    proxy._session_registry.get.return_value = None

    with pytest.raises(SessionDoesNotExistError, match="worker"):
        await proxy.run_in_session(SandboxBashAction(command="pwd", session="worker", sandbox_id="sbx-1"))

    backend.run_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_session_releases_mapping_only_after_remote_delete(service):
    proxy, backend = service
    proxy._session_registry.get.return_value = "os-session-1"
    backend.close_session.return_value = SimpleNamespace(session_type="bash")
    request = SandboxCloseBashSessionRequest(session="worker", sandbox_id="sbx-1")

    response = await proxy.close_session(request)

    assert response.session_type == "bash"
    backend.close_session.assert_awaited_once_with("sbx-1", _info(), "os-session-1")
    proxy._session_registry.remove.assert_awaited_once_with("sbx-1", "worker", "os-session-1")


@pytest.mark.asyncio
async def test_close_session_keeps_mapping_when_remote_delete_fails(service):
    proxy, backend = service
    proxy._session_registry.get.return_value = "os-session-1"
    backend.close_session.side_effect = RuntimeError("delete failed")

    with pytest.raises(RuntimeError, match="delete failed"):
        await proxy.close_session(SandboxCloseBashSessionRequest(session="worker", sandbox_id="sbx-1"))

    proxy._session_registry.remove.assert_not_awaited()


class FakeHTTPResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self):
        return b'{"ok":true}'

    def json(self):
        return {"ok": True}

    async def aclose(self):
        pass


class FakeHTTPClient:
    def __init__(self):
        self.request = None

    def build_request(self, **kwargs):
        self.request = kwargs
        return kwargs

    async def send(self, request, stream=False):
        assert stream is True
        return FakeHTTPResponse()


@pytest.mark.asyncio
async def test_http_proxy_uses_default_endpoint_and_required_headers(service):
    proxy, backend = service
    backend.get_endpoint.return_value = SimpleNamespace(
        endpoint="sandbox-osb-1-8080.example.test",
        headers={"X-Sandbox-Token": "required", "X-Endpoint": "yes"},
    )
    client = FakeHTTPClient()
    proxy._proxy_client = client

    response = await proxy.http_proxy(
        sandbox_id="sbx-1",
        target_path="api/items",
        body=b'{"name":"rock"}',
        headers=Headers({"content-type": "application/json", "x-sandbox-token": "client"}),
        method="PATCH",
        query_string="page=2",
    )

    assert response.status_code == 200
    assert backend.get_endpoint.await_args.args == ("sbx-1", _info(), 8080)
    assert client.request["url"] == "https://sandbox-osb-1-8080.example.test/api/items?page=2"
    assert client.request["method"] == "PATCH"
    assert client.request["content"] == b'{"name":"rock"}'
    request_headers = {key.lower(): value for key, value in client.request["headers"].items()}
    assert request_headers["x-sandbox-token"] == "required"
    assert request_headers["x-endpoint"] == "yes"


@pytest.mark.asyncio
async def test_http_proxy_uses_custom_port_and_preserves_endpoint_query(service):
    proxy, backend = service
    backend.get_endpoint.return_value = SimpleNamespace(
        endpoint="https://proxy.example/base?endpoint_token=abc",
        headers={},
    )
    client = FakeHTTPClient()
    proxy._proxy_client = client

    await proxy.http_proxy(
        sandbox_id="sbx-1",
        target_path="health",
        body=None,
        headers=Headers({}),
        method="GET",
        port=9000,
        query_string="verbose=1",
    )

    assert backend.get_endpoint.await_args.args == ("sbx-1", _info(), 9000)
    assert client.request["url"] == "https://proxy.example/base/health?endpoint_token=abc&verbose=1"


@pytest.mark.parametrize("target_path", ["../admin/health", "%2e%2e/admin/health", "%252e%252e/admin/health"])
def test_service_url_rejects_path_traversal(service, target_path):
    proxy, _ = service

    with pytest.raises(BadRequestRockError, match="invalid proxy path"):
        proxy._service_url("https://proxy.example/sandboxes/osb-1/ports/7000", target_path)


class FakeClientWebSocket:
    def __init__(self):
        self.headers = {"Authorization": "client", "X-Trace": "trace"}
        self.subprotocols = []
        self.accept = AsyncMock()
        self.close = AsyncMock()


class FakeWebSocketContext:
    def __init__(self):
        self.websocket = SimpleNamespace(subprotocol=None)

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_websocket_proxy_uses_custom_endpoint_path_headers_and_scheme(service, monkeypatch):
    proxy, backend = service
    backend.get_endpoint.return_value = SimpleNamespace(
        endpoint="https://proxy.example/sandboxes/osb-1/ports/7000",
        headers={"Authorization": "endpoint", "X-Endpoint": "yes"},
    )
    connect = Mock(return_value=FakeWebSocketContext())
    monkeypatch.setattr("rock.sandbox.service.sandbox_proxy_service.websockets.connect", connect)
    proxy._forward_messages = AsyncMock()
    client_ws = FakeClientWebSocket()

    await proxy.websocket_proxy(client_ws, "sbx-1", "socket/connect", port=7000)

    assert backend.get_endpoint.await_args.args == ("sbx-1", _info(), 7000)
    assert connect.call_args.args[0] == "wss://proxy.example/sandboxes/osb-1/ports/7000/socket/connect"
    additional = {key.lower(): value for key, value in connect.call_args.kwargs["additional_headers"]}
    assert additional == {
        "authorization": "endpoint",
        "x-trace": "trace",
        "x-endpoint": "yes",
    }
    client_ws.accept.assert_awaited_once_with(subprotocol=None)


@pytest.mark.asyncio
async def test_websocket_proxy_filters_reserved_endpoint_headers(service, monkeypatch):
    proxy, backend = service
    backend.get_endpoint.return_value = SimpleNamespace(
        endpoint="sandbox-osb-1-8080.example.test",
        headers={
            "Authorization": "endpoint",
            "Upgrade": "h2c",
            "Sec-WebSocket-Key": "invalid",
            "Content-Length": "42",
        },
    )
    connect = Mock(return_value=FakeWebSocketContext())
    monkeypatch.setattr("rock.sandbox.service.sandbox_proxy_service.websockets.connect", connect)
    proxy._forward_messages = AsyncMock()

    await proxy.websocket_proxy(FakeClientWebSocket(), "sbx-1")

    additional = {key.lower(): value for key, value in connect.call_args.kwargs["additional_headers"]}
    assert additional == {
        "authorization": "endpoint",
        "x-trace": "trace",
    }


@pytest.mark.asyncio
async def test_pending_to_running_uses_rock_state_machine(service):
    proxy, backend = service
    pending = _info()
    pending["state"] = State.PENDING
    pending["state_history"] = []
    proxy._meta_store.get = AsyncMock(return_value=pending)
    backend.get_state.return_value = State.RUNNING

    response = await proxy.get_status("sbx-1")

    assert response.state == State.RUNNING
    updated = proxy._meta_store.update.await_args.args[1]
    assert updated["state"] == State.RUNNING
    assert updated["state_history"][-1]["from_state"] == State.PENDING
    assert updated["state_history"][-1]["to_state"] == State.RUNNING
    assert updated["state_history"][-1]["event"] == "alive"
