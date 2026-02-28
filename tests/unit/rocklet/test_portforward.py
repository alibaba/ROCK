"""Tests for rocklet portforward WebSocket endpoint."""
import pytest
from fastapi import FastAPI

from rock.rocklet.local_api import local_router


@pytest.fixture
def app():
    """Create test FastAPI app with local_router."""
    app = FastAPI()
    app.include_router(local_router, tags=["local"])
    return app


class TestPortForwardEndpoint:
    """Tests for /portforward WebSocket endpoint."""

    def test_portforward_endpoint_exists(self, app: FastAPI):
        """Test that /portforward endpoint is registered."""
        routes = [route.path for route in app.routes]
        assert "/portforward" in routes, "Portforward endpoint should be registered"