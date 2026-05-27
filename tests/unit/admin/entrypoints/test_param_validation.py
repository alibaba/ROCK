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
    body = resp.json()
    assert body["status"] == "Failed"
    # New contract: helper-returns-Response → result is None, error carries the reason.
    # This shape avoids the ResponseValidationError that used to happen when
    # handle_exceptions wrapped raised exceptions into RockResponse[T] endpoints.
    assert body["result"] is None
    assert "is required" in body["error"]


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


@pytest.mark.asyncio
async def test_sandbox_stop_empty_sandbox_id_returns_clean_failure(sandbox_app):
    """Regression: /stop is typed RockResponse[str]; the old raise-via-handle_exceptions
    path produced a ResponseValidationError because the wrapped result was a
    SandboxResponse, not a str. The early-return pattern keeps the response shape
    consistent with the declared response_model."""
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/stop", json={"sandbox_id": ""}))
        _assert_failed(await client.post("/stop", json={"sandbox_id": "   "}))


@pytest.mark.asyncio
async def test_sandbox_commit_empty_image_tag(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/commit",
            json={"sandbox_id": "sb-1", "image_tag": "", "username": "u", "password": "p"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Failed"
        assert "image_tag is required" in resp.json()["error"]


@pytest.mark.asyncio
async def test_sandbox_run_in_session_empty_sandbox_id(sandbox_app):
    """Regression for issue 1: /run_in_session takes sandbox_id from the request body
    and was previously unvalidated."""
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/run_in_session", json={"sandbox_id": "", "command": "ls"}))


@pytest.mark.asyncio
async def test_sandbox_execute_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/execute", json={"sandbox_id": "", "command": "ls"}))


@pytest.mark.asyncio
async def test_sandbox_create_session_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/create_session", json={"sandbox_id": ""}))


@pytest.mark.asyncio
async def test_sandbox_close_session_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/close_session", json={"sandbox_id": "", "session": "s"}))


@pytest.mark.asyncio
async def test_sandbox_read_file_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/read_file", json={"sandbox_id": "", "path": "/tmp/x"}))


@pytest.mark.asyncio
async def test_sandbox_write_file_empty_sandbox_id(sandbox_app):
    app, _ = sandbox_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(
            await client.post("/write_file", json={"sandbox_id": "", "path": "/tmp/x", "content": "c"})
        )


# --- sandbox_proxy_api.py tests ---


@pytest.mark.asyncio
async def test_proxy_is_alive_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": ""}))
        _assert_failed(await client.get("/is_alive", params={"sandbox_id": "   "}))


@pytest.mark.asyncio
async def test_proxy_run_in_session_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/run_in_session", json={"sandbox_id": "", "command": "ls"}))


@pytest.mark.asyncio
async def test_proxy_execute_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/execute", json={"sandbox_id": "", "command": "ls"}))


@pytest.mark.asyncio
async def test_proxy_create_session_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/create_session", json={"sandbox_id": ""}))


@pytest.mark.asyncio
async def test_proxy_close_session_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/close_session", json={"sandbox_id": "", "session": "s"}))


@pytest.mark.asyncio
async def test_proxy_read_file_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/read_file", json={"sandbox_id": "", "path": "/tmp/x"}))


@pytest.mark.asyncio
async def test_proxy_write_file_empty_sandbox_id(proxy_app):
    app, _ = proxy_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(
            await client.post("/write_file", json={"sandbox_id": "", "path": "/tmp/x", "content": "c"})
        )
