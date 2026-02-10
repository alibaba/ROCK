from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service


@pytest.fixture
def app():
    mock_service = MagicMock()
    mock_service.post_proxy = AsyncMock(return_value={"ok": True})
    set_sandbox_proxy_service(mock_service)

    app = FastAPI()
    app.include_router(sandbox_proxy_router)
    return app, mock_service


@pytest.mark.asyncio
async def test_post_proxy_path_parsing(app):
    app, mock_service = app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No path
        await client.post("/sandboxes/sandbox-id/proxy", json={"key": "value"})
        args = mock_service.post_proxy.call_args
        assert args[0][0] == "sandbox-id"
        assert args[0][1] == ""
        assert args[0][2] == {"key": "value"}
        mock_service.post_proxy.reset_mock()

        # Single path segment
        await client.post("/sandboxes/sandbox-id/proxy/health", json={})
        args = mock_service.post_proxy.call_args
        assert args[0][0] == "sandbox-id"
        assert args[0][1] == "health"
        mock_service.post_proxy.reset_mock()

        # Nested path
        await client.post("/sandboxes/sandbox-id/proxy/api/v1/chat", json={"msg": "hi"})
        args = mock_service.post_proxy.call_args
        assert args[0][0] == "sandbox-id"
        assert args[0][1] == "api/v1/chat"
        assert args[0][2] == {"msg": "hi"}
        mock_service.post_proxy.reset_mock()

        # Deep nested path
        await client.post("/sandboxes/sandbox-id/proxy/a/b/c/d", json=None)
        args = mock_service.post_proxy.call_args
        assert args[0][0] == "sandbox-id"
        assert args[0][1] == "a/b/c/d"
