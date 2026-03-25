"""Tests for proxy enhancements:
1. WebSocket proxy supports user-specified port
2. HTTP proxy supports all HTTP methods
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_service():
    svc = MagicMock(spec=SandboxProxyService)
    svc.http_proxy = AsyncMock(return_value=JSONResponse({"ok": True}))
    svc.post_proxy = AsyncMock(return_value=JSONResponse({"ok": True}))
    svc.websocket_proxy = AsyncMock()
    set_sandbox_proxy_service(svc)
    return svc


@pytest.fixture
def app(mock_service):
    a = FastAPI()
    a.include_router(sandbox_proxy_router)
    return a, mock_service


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Proxy — all methods
# ─────────────────────────────────────────────────────────────────────────────


class TestHttpProxyAllMethods:
    """HTTP proxy endpoint should support GET, POST, PUT, DELETE, PATCH."""

    async def test_get_request_is_proxied(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.get("/sandboxes/sb1/proxy/status")

        svc.http_proxy.assert_called_once()
        call_kwargs = svc.http_proxy.call_args
        assert call_kwargs.kwargs["method"] == "GET" or call_kwargs.args[4] == "GET"

    async def test_post_request_is_proxied(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.post("/sandboxes/sb1/proxy/chat", json={"msg": "hi"})

        svc.http_proxy.assert_called_once()
        call = svc.http_proxy.call_args
        method = call.kwargs.get("method") or call.args[4]
        assert method == "POST"

    async def test_put_request_is_proxied(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.put("/sandboxes/sb1/proxy/items/1", json={"val": 42})

        svc.http_proxy.assert_called_once()
        call = svc.http_proxy.call_args
        method = call.kwargs.get("method") or call.args[4]
        assert method == "PUT"

    async def test_delete_request_is_proxied(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.delete("/sandboxes/sb1/proxy/items/1")

        svc.http_proxy.assert_called_once()
        call = svc.http_proxy.call_args
        method = call.kwargs.get("method") or call.args[4]
        assert method == "DELETE"

    async def test_patch_request_is_proxied(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.patch("/sandboxes/sb1/proxy/items/1", json={"val": 1})

        svc.http_proxy.assert_called_once()
        call = svc.http_proxy.call_args
        method = call.kwargs.get("method") or call.args[4]
        assert method == "PATCH"

    async def test_sandbox_id_and_path_are_passed_correctly(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.get("/sandboxes/my-sandbox/proxy/api/v1/health")

        call = svc.http_proxy.call_args
        # First positional arg is sandbox_id, second is path
        sandbox_id = call.args[0] if call.args else call.kwargs.get("sandbox_id")
        path = call.args[1] if len(call.args) > 1 else call.kwargs.get("target_path")
        assert sandbox_id == "my-sandbox"
        assert path == "api/v1/health"

    async def test_get_with_no_body_passes_none(self, app):
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.get("/sandboxes/sb1/proxy/items")

        call = svc.http_proxy.call_args
        body = call.args[2] if len(call.args) > 2 else call.kwargs.get("body")
        assert body is None

    async def test_port_param_is_passed_to_service(self, app):
        """When port=9000 is given, service.http_proxy should receive port=9000."""
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.get("/sandboxes/sb1/proxy/status?port=9000")

        svc.http_proxy.assert_called_once()
        call = svc.http_proxy.call_args
        port = call.kwargs.get("port") or (call.args[5] if len(call.args) > 5 else None)
        assert port == 9000

    async def test_port_defaults_to_none_when_not_given(self, app):
        """When port is not specified, service.http_proxy should receive port=None."""
        a, svc = app
        async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as client:
            await client.get("/sandboxes/sb1/proxy/status")

        call = svc.http_proxy.call_args
        port = call.kwargs.get("port") or (call.args[5] if len(call.args) > 5 else None)
        assert port is None


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Proxy — port parameter
# ─────────────────────────────────────────────────────────────────────────────


class TestWebsocketProxyPortParam:
    """WebSocket proxy endpoint should accept an optional port query parameter."""

    async def test_websocket_proxy_passes_port_to_service(self, app):
        """When port=8888 is given, service.websocket_proxy should receive port=8888."""
        a, svc = app
        client = TestClientWS(a)
        with client.websocket_connect("/sandboxes/sb1/proxy/ws?port=8888"):
            pass

        svc.websocket_proxy.assert_called_once()
        call = svc.websocket_proxy.call_args
        port = call.kwargs.get("port") or (call.args[3] if len(call.args) > 3 else None)
        assert port == 8888

    async def test_websocket_proxy_defaults_to_none_when_no_port(self, app):
        """When port is not specified, service.websocket_proxy should receive port=None."""
        a, svc = app
        client = TestClientWS(a)
        with client.websocket_connect("/sandboxes/sb1/proxy/ws"):
            pass

        svc.websocket_proxy.assert_called_once()
        call = svc.websocket_proxy.call_args
        port = call.kwargs.get("port") or (call.args[3] if len(call.args) > 3 else None)
        assert port is None

    async def test_websocket_proxy_rejects_invalid_port(self, app):
        """When port < 1024, websocket connection should close with code 1008."""
        a, svc = app
        client = TestClientWS(a)
        # Port 80 is below 1024 — expect rejection without calling service
        try:
            with client.websocket_connect("/sandboxes/sb1/proxy/ws?port=80"):
                pass
        except Exception:
            pass  # Expect disconnect

        # Service should NOT be called for invalid port
        svc.websocket_proxy.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# SandboxProxyService — get_sandbox_websocket_url with port
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSandboxWebsocketUrl:
    """Service method get_sandbox_websocket_url should use provided port."""

    async def test_uses_provided_port_when_specified(self):
        """When port is given, URL should use that port directly."""
        from rock.deployments.constants import Port
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service.get_service_status = AsyncMock(
            return_value=[{"host_ip": "10.0.0.1", "ports": {str(Port.SERVER.value): 32000}}]
        )

        # Call the real method
        url = await SandboxProxyService.get_sandbox_websocket_url(service, "sb1", "api/ws", port=8888)
        assert url == "ws://10.0.0.1:8888/api/ws"

    async def test_uses_mapped_server_port_when_no_port(self):
        """When port is None, URL should use mapped SERVER port."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 32000

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockServiceStatus:
            MockServiceStatus.from_dict.return_value = mock_status
            service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

            url = await SandboxProxyService.get_sandbox_websocket_url(service, "sb1", None, port=None)

        assert url == "ws://10.0.0.1:32000"
        mock_status.get_mapped_port.assert_called_once_with(Port.SERVER)


# ─────────────────────────────────────────────────────────────────────────────
# SandboxProxyService — http_proxy with method
# ─────────────────────────────────────────────────────────────────────────────


class TestHttpProxyServiceMethod:
    """Service http_proxy should use the provided method when building request."""

    async def test_http_proxy_uses_provided_method(self):
        """http_proxy should send request with the given method."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 8080
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_response = MagicMock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}
        mock_response.aread = AsyncMock(return_value=b'{"result": "ok"}')
        mock_response.aclose = AsyncMock()

        sent_method = {}

        class FakeClient:
            def build_request(self, method, **kwargs):
                sent_method["method"] = method
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=FakeClient()):
                await SandboxProxyService.http_proxy(
                    service,
                    sandbox_id="sb1",
                    target_path="items",
                    body=None,
                    headers=Headers({}),
                    method="DELETE",
                )

        assert sent_method["method"] == "DELETE"

    async def test_http_proxy_defaults_to_post(self):
        """http_proxy without method argument should default to POST."""
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 8080

        mock_response = MagicMock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.aread = AsyncMock(return_value=b"{}")
        mock_response.aclose = AsyncMock()

        sent_method = {}

        class FakeClient:
            def build_request(self, method, **kwargs):
                sent_method["method"] = method
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=FakeClient()):
                await SandboxProxyService.http_proxy(
                    service,
                    sandbox_id="sb1",
                    target_path="",
                    body=None,
                    headers=Headers({}),
                )

        assert sent_method["method"] == "POST"

    async def test_http_proxy_uses_provided_port(self):
        """http_proxy should build target URL with the given port."""
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 8080

        mock_response = MagicMock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.aread = AsyncMock(return_value=b"{}")
        mock_response.aclose = AsyncMock()

        built_url = {}

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built_url["url"] = url
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=FakeClient()):
                await SandboxProxyService.http_proxy(
                    service,
                    sandbox_id="sb1",
                    target_path="api/test",
                    body=None,
                    headers=Headers({}),
                    port=9000,
                )

        assert "9000" in built_url["url"]
        # Should NOT use mapped port when port is explicitly provided
        mock_status.get_mapped_port.assert_not_called()

    async def test_http_proxy_uses_mapped_port_when_none(self):
        """http_proxy without port should use the mapped SERVER port."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 32000

        mock_response = MagicMock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.aread = AsyncMock(return_value=b"{}")
        mock_response.aclose = AsyncMock()

        built_url = {}

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built_url["url"] = url
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def aclose(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=FakeClient()):
                await SandboxProxyService.http_proxy(
                    service,
                    sandbox_id="sb1",
                    target_path="",
                    body=None,
                    headers=Headers({}),
                )

        assert "32000" in built_url["url"]
        mock_status.get_mapped_port.assert_called_once_with(Port.SERVER)


# ─────────────────────────────────────────────────────────────────────────────
# Helper — sync WebSocket test client wrapper
# ─────────────────────────────────────────────────────────────────────────────


class TestClientWS:
    """Thin wrapper around FastAPI TestClient for WebSocket connections."""

    def __init__(self, app):
        from fastapi.testclient import TestClient

        self._client = TestClient(app, raise_server_exceptions=False)

    def websocket_connect(self, path):
        return self._client.websocket_connect(path)
