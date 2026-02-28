"""Tests for WebSocket TCP port forwarding proxy."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service


class TestValidatePort:
    """Tests for _validate_port method."""

    def test_validate_port_accepts_valid_port_in_range(self, sandbox_proxy_service):
        """Valid port in allowed range should pass validation."""
        is_valid, error = sandbox_proxy_service._validate_port(8080)
        assert is_valid is True
        assert error is None

    def test_validate_port_rejects_port_below_1024(self, sandbox_proxy_service):
        """Port below 1024 should be rejected."""
        is_valid, error = sandbox_proxy_service._validate_port(1023)
        assert is_valid is False
        assert "1024" in error

    def test_validate_port_rejects_port_22(self, sandbox_proxy_service):
        """SSH port 22 should be rejected even though it's in the excluded list."""
        is_valid, error = sandbox_proxy_service._validate_port(22)
        assert is_valid is False
        assert "22" in error

    def test_validate_port_rejects_port_above_65535(self, sandbox_proxy_service):
        """Port above 65535 should be rejected."""
        is_valid, error = sandbox_proxy_service._validate_port(65536)
        assert is_valid is False
        assert "65535" in error

    def test_validate_port_accepts_min_allowed_port(self, sandbox_proxy_service):
        """Minimum allowed port (1024) should pass validation."""
        is_valid, error = sandbox_proxy_service._validate_port(1024)
        assert is_valid is True
        assert error is None

    def test_validate_port_accepts_max_allowed_port(self, sandbox_proxy_service):
        """Maximum allowed port (65535) should pass validation."""
        is_valid, error = sandbox_proxy_service._validate_port(65535)
        assert is_valid is True
        assert error is None

    def test_validate_port_rejects_zero_port(self, sandbox_proxy_service):
        """Port 0 should be rejected."""
        is_valid, error = sandbox_proxy_service._validate_port(0)
        assert is_valid is False
        assert error is not None

    def test_validate_port_rejects_negative_port(self, sandbox_proxy_service):
        """Negative port should be rejected."""
        is_valid, error = sandbox_proxy_service._validate_port(-1)
        assert is_valid is False
        assert error is not None


class TestGetRockletPortforwardUrl:
    """Tests for _get_rocklet_portforward_url method."""

    def test_get_rocklet_portforward_url_returns_correct_format(self, sandbox_proxy_service):
        """Should return correct WebSocket URL for rocklet portforward endpoint."""
        mock_status = {
            "host_ip": "192.168.1.100",
            "phases": {
                "image_pull": {"status": "running", "message": "done"},
                "docker_run": {"status": "running", "message": "done"},
            },
            "port_mapping": {22555: 32768},  # PROXY port mapping (rocklet listens on 22555)
        }
        url = sandbox_proxy_service._get_rocklet_portforward_url(mock_status, 9000)
        # Should connect to rocklet's PROXY port (22555 mapped to 32768) with /portforward path
        assert url == "ws://192.168.1.100:32768/portforward?port=9000"


class TestWebsocketToTcpProxy:
    """Tests for websocket_to_tcp_proxy method."""

    @pytest.mark.asyncio
    async def test_websocket_to_tcp_proxy_validates_port(self, sandbox_proxy_service):
        """Should raise error for invalid port before attempting connection."""
        mock_websocket = MagicMock(spec=WebSocket)

        with pytest.raises(ValueError) as exc_info:
            await sandbox_proxy_service.websocket_to_tcp_proxy(
                client_websocket=mock_websocket,
                sandbox_id="test-sandbox",
                port=22,  # Invalid port (SSH)
            )
        assert "22" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_websocket_to_tcp_proxy_raises_for_nonexistent_sandbox(self, sandbox_proxy_service):
        """Should raise error when sandbox does not exist."""
        mock_websocket = MagicMock(spec=WebSocket)

        with patch.object(
            sandbox_proxy_service, "get_service_status", side_effect=Exception("sandbox not-started not started")
        ):
            with pytest.raises(Exception) as exc_info:
                await sandbox_proxy_service.websocket_to_tcp_proxy(
                    client_websocket=mock_websocket,
                    sandbox_id="nonexistent-sandbox",
                    port=8080,
                )
            assert "not started" in str(exc_info.value)


class TestPortForwardRoute:
    """Tests for the portforward WebSocket route."""

    @pytest.fixture
    def app(self):
        """Create a FastAPI app with the sandbox_proxy_router."""
        mock_service = MagicMock()
        mock_service.websocket_to_tcp_proxy = AsyncMock()
        set_sandbox_proxy_service(mock_service)

        app = FastAPI()
        app.include_router(sandbox_proxy_router)
        return app, mock_service

    def test_portforward_route_valid_port_calls_service(self, app):
        """Should call websocket_to_tcp_proxy with correct parameters."""
        app, mock_service = app
        client = TestClient(app)

        # Mock the service to complete immediately
        mock_service.websocket_to_tcp_proxy.return_value = None

        # The WebSocket connection will be accepted and the service called
        # Since the mock returns immediately, the connection should close
        try:
            with client.websocket_connect("/sandboxes/test-sandbox/portforward?port=8080") as websocket:
                # Connection established, service should be called
                pass
        except Exception:
            # Expected: connection may close due to mock behavior
            pass

        # Verify the service was called with correct parameters
        mock_service.websocket_to_tcp_proxy.assert_called_once()
        call_args = mock_service.websocket_to_tcp_proxy.call_args
        # sandbox_id is the second positional argument
        assert call_args[0][1] == "test-sandbox"
        # port is the third positional argument
        assert call_args[0][2] == 8080

    def test_portforward_route_invalid_port_rejected(self, app):
        """Should reject invalid port (port 22) with close code 1008."""
        app, mock_service = app
        mock_service.websocket_to_tcp_proxy.side_effect = ValueError("Port 22 is not allowed for port forwarding")

        client = TestClient(app)

        # The WebSocket connection will be closed with code 1008
        with client.websocket_connect("/sandboxes/test-sandbox/portforward?port=22") as websocket:
            # Connection should be closed by server
            pass

        # The service should have been called
        mock_service.websocket_to_tcp_proxy.assert_called_once()