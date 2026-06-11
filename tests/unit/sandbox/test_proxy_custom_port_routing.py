"""Test that custom ports are routed through Rocklet (Port.PROXY) instead of direct connect.

Regression test for the bug where http_proxy and websocket_proxy would attempt to
connect directly to host_ip:{custom_port}, which fails because arbitrary container
ports are not mapped to the host. The fix routes custom ports through Rocklet's
/proxy/{port}/{path} endpoint which lives inside the container.
"""

from unittest.mock import AsyncMock, patch

import pytest

from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


@pytest.fixture
def mock_meta_store():
    store = AsyncMock()
    store.get = AsyncMock(
        return_value={
            "host_ip": "10.0.0.1",
            "ports": {
                str(Port.SSH): 22,
                str(Port.PROXY): 22555,
                str(Port.SERVER): 8080,
            },
        }
    )
    store.get_timeout = AsyncMock(return_value=None)
    return store


@pytest.fixture
def proxy_service(mock_meta_store, tmp_path):
    config = AsyncMock()
    config.proxy_service.timeout = 30
    config.proxy_service.max_connections = 100
    config.proxy_service.max_keepalive_connections = 20
    config.oss.access_key_id = ""
    config.oss.access_key_secret = ""
    config.oss.role_arn = ""
    config.oss.endpoint = ""
    config.oss.bucket = ""
    config.oss.region = ""
    config.oss.primary.access_key_id = ""
    config.oss.primary.access_key_secret = ""
    config.oss.primary.role_arn = ""
    config.oss.primary.endpoint = ""
    config.oss.primary.bucket = ""
    config.oss.primary.region = ""
    config.proxy_service.batch_get_status_max_count = 100
    config.runtime.metrics_endpoint = None
    config.runtime.user_defined_tags = {}
    config.sandbox_config.file_transfer.prefix = None

    service = SandboxProxyService.__new__(SandboxProxyService)
    service._meta_store = mock_meta_store
    service._rock_config = config
    service.oss_config = config.oss
    service.proxy_config = config.proxy_service
    return service


@pytest.mark.asyncio
async def test_http_proxy_custom_port_routes_via_rocklet(proxy_service):
    """When a custom port is specified, http_proxy should route through Rocklet."""
    with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as mock_status_cls:
        mock_status = mock_status_cls.from_dict.return_value
        mock_status.get_mapped_port.side_effect = lambda p: {
            Port.SERVER: 8080,
            Port.PROXY: 22555,
        }[p]

        # Mock httpx to capture the URL
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json = AsyncMock(return_value={"ok": True})
        mock_response.aread = AsyncMock(return_value=b'{"ok": true}')
        mock_response.aclose = AsyncMock()

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value=mock_response)
        mock_client.build_request = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=mock_client):
            from starlette.datastructures import Headers

            await proxy_service.http_proxy(
                sandbox_id="test-sandbox",
                target_path="api/test",
                body=None,
                headers=Headers({"content-type": "application/json"}),
                method="GET",
                port=9000,  # Custom port
                proxy_prefix="/sandboxes/test-sandbox/proxy",
            )

        # Verify the request went to Rocklet port 22555, not direct to 9000
        build_request_call = mock_client.build_request.call_args
        url = build_request_call.kwargs.get("url") or build_request_call.args[1]
        assert ":22555/" in url, f"Expected Rocklet port 22555 in URL, got: {url}"
        assert "/proxy/9000/" in url, f"Expected /proxy/9000/ in URL, got: {url}"
        assert ":9000/" not in url, f"Should not connect directly to port 9000: {url}"


@pytest.mark.asyncio
async def test_http_proxy_default_port_direct_connect(proxy_service):
    """When no port is specified (default 8080), http_proxy should connect directly."""
    with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as mock_status_cls:
        mock_status = mock_status_cls.from_dict.return_value
        mock_status.get_mapped_port.side_effect = lambda p: {
            Port.SERVER: 8080,
            Port.PROXY: 22555,
        }[p]

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json = AsyncMock(return_value={"ok": True})
        mock_response.aread = AsyncMock(return_value=b'{"ok": true}')
        mock_response.aclose = AsyncMock()

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value=mock_response)
        mock_client.build_request = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("rock.sandbox.service.sandbox_proxy_service.httpx.AsyncClient", return_value=mock_client):
            from starlette.datastructures import Headers

            await proxy_service.http_proxy(
                sandbox_id="test-sandbox",
                target_path="api/test",
                body=None,
                headers=Headers({"content-type": "application/json"}),
                method="GET",
                port=None,  # Default port
                proxy_prefix="/sandboxes/test-sandbox/proxy",
            )

        # Verify the request went directly to port 8080
        build_request_call = mock_client.build_request.call_args
        url = build_request_call.kwargs.get("url") or build_request_call.args[1]
        assert ":8080/" in url, f"Expected direct connect to port 8080, got: {url}"
        assert "/proxy/" not in url, f"Default port should not use /proxy/ route: {url}"


@pytest.mark.asyncio
async def test_websocket_url_custom_port_routes_via_rocklet(proxy_service):
    """When a custom port is specified, get_sandbox_websocket_url should route through Rocklet."""
    with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as mock_status_cls:
        mock_status = mock_status_cls.from_dict.return_value
        mock_status.get_mapped_port.side_effect = lambda p: {
            Port.SERVER: 8080,
            Port.PROXY: 22555,
        }[p]

        url = await proxy_service.get_sandbox_websocket_url(
            sandbox_id="test-sandbox",
            target_path="ws",
            port=9000,  # Custom port
        )

        # Verify the URL routes through Rocklet
        assert ":22555/" in url, f"Expected Rocklet port 22555 in URL, got: {url}"
        assert "/proxy/9000/" in url, f"Expected /proxy/9000/ in URL, got: {url}"
        assert ":9000/" not in url, f"Should not connect directly to port 9000: {url}"


@pytest.mark.asyncio
async def test_websocket_url_default_port_direct_connect(proxy_service):
    """When no port is specified (default 8080), get_sandbox_websocket_url should connect directly."""
    with patch("rock.sandbox.service.sandbox_proxy_service.ServiceStatus") as mock_status_cls:
        mock_status = mock_status_cls.from_dict.return_value
        mock_status.get_mapped_port.side_effect = lambda p: {
            Port.SERVER: 8080,
            Port.PROXY: 22555,
        }[p]

        url = await proxy_service.get_sandbox_websocket_url(
            sandbox_id="test-sandbox",
            target_path="ws",
            port=None,  # Default port
        )

        # Verify the URL connects directly to port 8080
        assert ":8080/" in url, f"Expected direct connect to port 8080, got: {url}"
        assert "/proxy/" not in url, f"Default port should not use /proxy/ route: {url}"
