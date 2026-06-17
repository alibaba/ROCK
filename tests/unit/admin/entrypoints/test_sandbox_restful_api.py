"""Tests for the RESTful sandbox restart endpoint on sandbox_router."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.proto.response import SandboxStartResponse
from rock.common.exception import request_validation_exception_handler


@pytest.fixture
def sandbox_app():
    mock_manager = MagicMock()
    mock_manager.rock_config = MagicMock()
    mock_manager.rock_config.nacos_provider = None
    set_sandbox_manager(mock_manager)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.include_router(sandbox_router, prefix="/apis/envs/sandbox/v1")
    return app, mock_manager


BASE = "/apis/envs/sandbox/v1"


@pytest.mark.asyncio
async def test_restart_restful(sandbox_app):
    app, mock_manager = sandbox_app
    mock_manager.restart_async = AsyncMock(
        return_value=SandboxStartResponse(sandbox_id="sb-1", host_name="h", host_ip="1.2.3.4")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"{BASE}/sandboxes/sb-1/restart")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "Success"
        assert body["result"]["sandbox_id"] == "sb-1"
    mock_manager.restart_async.assert_called_once_with("sb-1")
