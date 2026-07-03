"""Tests for RESTful sandbox endpoints on sandbox_router."""

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


@pytest.mark.asyncio
async def test_archive_allowed(sandbox_app):
    app, mock_manager = sandbox_app
    mock_manager.rock_config.lifecycle.archive.enabled = True
    mock_manager.rock_config.lifecycle.archive.is_allowed.return_value = True
    mock_manager.archive_sandbox = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"{BASE}/sandboxes/sb-1/archive", headers={"X-Key": "good-key"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "Success"
    mock_manager.archive_sandbox.assert_called_once_with("sb-1")


@pytest.mark.asyncio
async def test_archive_forbidden(sandbox_app):
    """Unauthorised key: API layer rejects before calling manager."""
    app, mock_manager = sandbox_app
    mock_manager.rock_config.lifecycle.archive.enabled = True
    mock_manager.rock_config.lifecycle.archive.is_allowed.return_value = False
    mock_manager.archive_sandbox = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"{BASE}/sandboxes/sb-1/archive", headers={"X-Key": "bad-key"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "Failed"
    mock_manager.archive_sandbox.assert_not_called()
