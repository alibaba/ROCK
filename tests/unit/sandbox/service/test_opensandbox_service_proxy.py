from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.datastructures import Headers

from rock.actions.sandbox.response import State
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
    result = OpenSandboxProxyService.__new__(OpenSandboxProxyService)
    result._opensandbox_backend = backend
    result._opensandbox_protocol = "https"
    result._meta_store = SimpleNamespace(get=AsyncMock(return_value=_info()), update=AsyncMock())
    result._update_expire_time = AsyncMock()
    return result, backend


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
