"""Test parameter validation at the API endpoint level."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.gem.api import gem_router, set_env_service
from rock.admin.proto.response import SandboxStartResponse
from rock.common.exception import request_validation_exception_handler


def _build_app(router):
    app = FastAPI()
    # Match the production app: pydantic validation errors are mapped back to
    # the RockResponse envelope instead of FastAPI's default 422 {"detail": [...]}.
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.include_router(router)
    return app


@pytest.fixture
def sandbox_app():
    mock_manager = MagicMock()
    mock_manager.rock_config = MagicMock()
    mock_manager.rock_config.nacos_provider = None
    mock_manager.rock_config.lifecycle.default_startup_timeout_seconds = 600
    mock_manager.rock_config.lifecycle.min_startup_timeout_seconds = 600
    mock_manager.rock_config.lifecycle.max_startup_timeout_seconds = 1800
    mock_manager.rock_config.runtime.sandbox_disk_limit_rootfs = None
    mock_manager.rock_config.runtime.sandbox_disk_overcommit_ratio = None
    set_sandbox_manager(mock_manager)
    return _build_app(sandbox_router), mock_manager


@pytest.fixture
def proxy_app():
    mock_service = MagicMock()
    set_sandbox_proxy_service(mock_service)
    return _build_app(sandbox_proxy_router), mock_service


@pytest.fixture
def gem_app():
    mock_service = MagicMock()
    set_env_service(mock_service)
    return _build_app(gem_router), mock_service


def _assert_failed(resp, *, field: str = "sandbox_id"):
    """Assert a RockResponse-shaped failure mentioning ``field`` in the error.

    Validation now happens at deserialization via NonBlankStr. The global
    RequestValidationError handler maps the failure back to RockResponse with
    HTTP 200, ``status=Failed``, ``result=None``, and a ``error`` string that
    embeds the offending field's location (e.g. ``body.sandbox_id: ...``).
    """
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is None
    assert field in body["error"]


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
        _assert_failed(await client.post("/start", json={"image": ""}), field="image")
        _assert_failed(await client.post("/start", json={"image": "   "}), field="image")
        _assert_failed(await client.post("/start", json={}), field="image")


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
        _assert_failed(
            await client.post(
                "/commit",
                json={"sandbox_id": "sb-1", "image_tag": "", "username": "u", "password": "p"},
            ),
            field="image_tag",
        )


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
        _assert_failed(await client.post("/write_file", json={"sandbox_id": "", "path": "/tmp/x", "content": "c"}))


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
        _assert_failed(await client.post("/write_file", json={"sandbox_id": "", "path": "/tmp/x", "content": "c"}))


# --- gem/api.py tests ---


@pytest.mark.asyncio
async def test_gem_step_empty_sandbox_id(gem_app):
    app, _ = gem_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/step", json={"sandbox_id": "", "action": "noop"}))
        _assert_failed(await client.post("/step", json={"sandbox_id": "   ", "action": "noop"}))


@pytest.mark.asyncio
async def test_gem_reset_empty_sandbox_id(gem_app):
    app, _ = gem_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/reset", json={"sandbox_id": ""}))
        _assert_failed(await client.post("/reset", json={"sandbox_id": "   "}))


@pytest.mark.asyncio
async def test_gem_close_empty_sandbox_id(gem_app):
    app, _ = gem_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _assert_failed(await client.post("/close", json={"sandbox_id": ""}))
        _assert_failed(await client.post("/close", json={"sandbox_id": "   "}))
