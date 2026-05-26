"""Test parameter validation at the API endpoint level."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.proto.response import SandboxStartResponse


@pytest.fixture
def sandbox_app():
    mock_manager = MagicMock()
    mock_manager.rock_config = MagicMock()
    mock_manager.rock_config.nacos_provider = None
    set_sandbox_manager(mock_manager)
    app = FastAPI()
    app.include_router(sandbox_router)
    return app, mock_manager


@pytest.fixture
def proxy_app():
    mock_service = MagicMock()
    set_sandbox_proxy_service(mock_service)
    app = FastAPI()
    app.include_router(sandbox_proxy_router)
    return app, mock_service


def _assert_failed(resp):
    assert resp.status_code == 200
    assert resp.json()["status"] == "Failed"


# --- sandbox_api.py tests ---


@pytest.mark.asyncio
async def test_sandbox_is_alive_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": ""}))
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": "   "}))


@pytest.mark.asyncio
async def test_sandbox_get_status_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.get("/get_status", params={"sandbox_id": ""}))


@pytest.mark.asyncio
async def test_sandbox_get_statistics_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.get("/get_sandbox_statistics", params={"sandbox_id": ""}))


@pytest.mark.asyncio
async def test_sandbox_start_empty_image(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/start", json={"image": ""}))
        _assert_failed(await client.post("/start", json={"image": "   "}))


@pytest.mark.asyncio
async def test_sandbox_start_valid_image_passes_validation(sandbox_app):
    """Verify that a valid image value does not trigger the validation error."""
    app, mock_manager = sandbox_app
    mock_manager.start = AsyncMock(
        return_value=SandboxStartResponse(sandbox_id="sb-1", host_name="h", host_ip="1.2.3.4")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/start", json={"image": "python:3.11"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "Success"


# --- sandbox_proxy_api.py tests ---


@pytest.mark.asyncio
async def test_proxy_is_alive_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": ""}))
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": "   "}))
