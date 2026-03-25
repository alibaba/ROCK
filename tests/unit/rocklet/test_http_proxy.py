"""Tests for rocklet /http_proxy endpoint."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from rock.rocklet.local_api import local_router


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(local_router)
    return a


class TestHttpProxyEndpointExists:
    def test_http_proxy_routes_registered(self, app):
        routes = [route.path for route in app.routes]
        assert "/http_proxy" in routes
        assert "/http_proxy/{path:path}" in routes


class TestHttpProxyForwarding:
    """rocklet /http_proxy should forward requests to localhost:{port}/{path}."""

    async def test_get_request_forwarded_to_target_port(self, app):
        """GET /http_proxy/api/status?port=9000 should forward to localhost:9000/api/status."""
        built = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{"ok": true}')
        mock_response.aclose = AsyncMock()
        mock_response.json = MagicMock(return_value={"ok": True})

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built["method"] = method
                built["url"] = url
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("rock.rocklet.local_api.httpx.AsyncClient", return_value=FakeClient()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.get("/http_proxy/api/status?port=9000")

        assert built["method"] == "GET"
        assert "localhost:9000" in built["url"]
        assert "api/status" in built["url"]

    async def test_post_request_forwarded_with_body(self, app):
        """POST /http_proxy/chat?port=8888 should forward body to localhost:8888/chat."""
        built = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{}')
        mock_response.aclose = AsyncMock()
        mock_response.json = MagicMock(return_value={})

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built["method"] = method
                built["url"] = url
                built["content"] = kwargs.get("content")
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("rock.rocklet.local_api.httpx.AsyncClient", return_value=FakeClient()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/http_proxy/chat?port=8888", json={"msg": "hi"})

        assert built["method"] == "POST"
        assert "localhost:8888" in built["url"]

    async def test_delete_request_forwarded(self, app):
        """DELETE /http_proxy/items/1?port=9000 should forward to localhost:9000/items/1."""
        built = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{}')
        mock_response.aclose = AsyncMock()
        mock_response.json = MagicMock(return_value={})

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built["method"] = method
                built["url"] = url
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("rock.rocklet.local_api.httpx.AsyncClient", return_value=FakeClient()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.delete("/http_proxy/items/1?port=9000")

        assert built["method"] == "DELETE"
        assert "localhost:9000" in built["url"]
        assert "items/1" in built["url"]

    async def test_no_path_variant(self, app):
        """GET /http_proxy?port=9000 (no path) should forward to localhost:9000/."""
        built = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{}')
        mock_response.aclose = AsyncMock()
        mock_response.json = MagicMock(return_value={})

        class FakeClient:
            def build_request(self, method, url, **kwargs):
                built["url"] = url
                return MagicMock()

            async def send(self, req, stream=False):
                return mock_response

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("rock.rocklet.local_api.httpx.AsyncClient", return_value=FakeClient()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.get("/http_proxy?port=9000")

        assert "localhost:9000" in built["url"]


class TestHttpProxyAdminSideRouting:
    """Admin http_proxy service: with port → route via rocklet /http_proxy."""

    async def test_http_proxy_with_port_routes_via_rocklet(self):
        """When port is given, target URL should use rocklet mapped port + /http_proxy path."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.side_effect = lambda p: 32555 if p == Port.PROXY else 32080

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

            async def aclose(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        from starlette.datastructures import Headers

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

        # Should use rocklet PROXY port, not SERVER port
        assert "32555" in built_url["url"]
        assert "http_proxy" in built_url["url"]
        assert "port=9000" in built_url["url"]

    async def test_http_proxy_without_port_uses_server_port(self):
        """When port is None, target URL should use mapped SERVER port directly."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        service._update_expire_time = AsyncMock()
        service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])

        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 32080

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

            async def aclose(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        from starlette.datastructures import Headers

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=FakeClient()):
                await SandboxProxyService.http_proxy(
                    service,
                    sandbox_id="sb1",
                    target_path="api/test",
                    body=None,
                    headers=Headers({}),
                )

        assert "32080" in built_url["url"]
        assert "http_proxy" not in built_url["url"]
        mock_status.get_mapped_port.assert_called_with(Port.SERVER)


class TestWebsocketProxyCustomPortRoutesViaRocklet:
    """Admin websocket_proxy: with port → URL should use rocklet portforward."""

    async def test_websocket_url_with_port_uses_rocklet_portforward(self):
        """get_sandbox_websocket_url with port should return rocklet portforward URL."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.side_effect = lambda p: 32555 if p == Port.PROXY else 32080

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])
            url = await SandboxProxyService.get_sandbox_websocket_url(service, "sb1", None, port=8888)

        assert "32555" in url
        assert "portforward" in url
        assert "port=8888" in url
        # Should NOT use SERVER port
        assert "32080" not in url

    async def test_websocket_url_without_port_uses_server_port(self):
        """get_sandbox_websocket_url without port should return direct SERVER port URL."""
        from rock.deployments.constants import Port
        from rock.deployments.status import ServiceStatus
        from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

        service = MagicMock(spec=SandboxProxyService)
        mock_status = MagicMock(spec=ServiceStatus)
        mock_status.get_mapped_port.return_value = 32080

        with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as MockSS:
            MockSS.from_dict.return_value = mock_status
            service.get_service_status = AsyncMock(return_value=[{"host_ip": "10.0.0.1"}])
            url = await SandboxProxyService.get_sandbox_websocket_url(service, "sb1", None, port=None)

        assert "ws://10.0.0.1:32080" == url
        assert "portforward" not in url
        mock_status.get_mapped_port.assert_called_with(Port.SERVER)
