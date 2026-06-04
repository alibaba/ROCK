"""Tests for global exception handlers shared across FastAPI apps."""

from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, StringConstraints

from rock.common.exception import request_validation_exception_handler


class _Body(BaseModel):
    sandbox_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@pytest.fixture
def app():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.post("/echo")
    async def _echo(body: _Body):
        return {"ok": True}

    @app.get("/echo_query")
    async def _echo_query(sandbox_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]):
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_empty_string_body_returns_rock_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/echo", json={"sandbox_id": ""})
    # Envelope contract aligns with validate_required_str: HTTP 200, business failure inside body.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is None
    assert "sandbox_id" in body["error"]


@pytest.mark.asyncio
async def test_whitespace_only_body_returns_rock_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/echo", json={"sandbox_id": "   "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is None
    assert "sandbox_id" in body["error"]


@pytest.mark.asyncio
async def test_missing_field_returns_rock_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/echo", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is None
    assert "sandbox_id" in body["error"]


@pytest.mark.asyncio
async def test_invalid_query_param_returns_rock_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/echo_query", params={"sandbox_id": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is None
    assert "sandbox_id" in body["error"]


@pytest.mark.asyncio
async def test_valid_request_passes_through(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/echo", json={"sandbox_id": "abc"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# handle_exceptions: pin the envelope contract across response_model variants.
#
# Migration goal: the structured error `code` lives on the envelope itself
# (RockResponse.code) — new SDKs should read it from there. The legacy
# `result=SandboxResponse(code=...)` payload is preserved for backward-compat.
# Endpoints that return simple values use bare `RockResponse` (not
# `RockResponse[str]`) so the error SandboxResponse result passes validation.
# ---------------------------------------------------------------------------

from rock._codes import codes  # noqa: E402
from rock.actions import RockResponse, SandboxResponse  # noqa: E402
from rock.common.exception import handle_exceptions  # noqa: E402
from rock.sdk.common.exceptions import BadRequestRockError  # noqa: E402


class _ChildResponse(SandboxResponse):
    """Stand-in for SandboxStartResponse-style models that inherit from
    SandboxResponse and add optional fields. Used to prove handle_exceptions
    still populates a SandboxResponse-shaped result for backward-compat with
    SDKs that parse result.code on /start_async-style endpoints."""

    sandbox_id: str | None = None
    host_name: str | None = None


@pytest.fixture
def handle_exc_app():
    app = FastAPI()

    @app.post("/stop_rock_exc")
    @handle_exceptions(error_message="stop sandbox failed")
    async def _stop_rock_exc() -> RockResponse:
        raise BadRequestRockError("bad sandbox id")

    @app.post("/stop_generic_exc")
    @handle_exceptions(error_message="stop sandbox failed")
    async def _stop_generic_exc() -> RockResponse:
        raise RuntimeError("kaboom")

    @app.post("/stop_ok")
    @handle_exceptions(error_message="stop sandbox failed")
    async def _stop_ok() -> RockResponse:
        return RockResponse(result="abc stopped")

    @app.post("/start_rock_exc")
    @handle_exceptions(error_message="start sandbox failed")
    async def _start_rock_exc() -> RockResponse[_ChildResponse]:
        raise BadRequestRockError("invalid config")

    @app.post("/start_generic_exc")
    @handle_exceptions(error_message="start sandbox failed")
    async def _start_generic_exc() -> RockResponse[_ChildResponse]:
        raise RuntimeError("kaboom")

    @app.post("/ok")
    @handle_exceptions()
    async def _ok() -> RockResponse:
        return RockResponse(result="hello")

    return app


@pytest.mark.asyncio
async def test_rock_response_str_with_rock_exception_no_validation_error(handle_exc_app):
    """RockException on a bare RockResponse endpoint: envelope carries the
    structured error code, and result is populated with SandboxResponse for
    backward-compat with older SDKs."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/stop_rock_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["message"] == "stop sandbox failed"
    # New path: code is on the envelope.
    assert body["code"] == int(codes.BAD_REQUEST)
    assert "bad sandbox id" in body["error"]
    # Backward-compat path: result is populated even for RockResponse[str].
    # Old SDK does `SandboxResponse(**result)` -> raise_for_code(code).
    assert body["result"] is not None
    sandbox_resp = SandboxResponse(**body["result"])
    assert sandbox_resp.code == codes.BAD_REQUEST
    assert sandbox_resp.failure_reason == "bad sandbox id"


@pytest.mark.asyncio
async def test_rock_response_str_with_generic_exception_result_is_none(handle_exc_app):
    """Generic Exception: result is None (no SandboxResponse), error info
    lives on envelope fields only."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/stop_generic_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["message"] == "stop sandbox failed"
    assert body.get("code") is None
    assert "kaboom" in body["error"]
    assert body["result"] is None


@pytest.mark.asyncio
async def test_rock_response_str_success_path_unchanged(handle_exc_app):
    """Wrapping with handle_exceptions must not break the success path: the
    declared T (str) still validates and serializes."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/stop_ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Success"
    assert body["result"] == "abc stopped"


@pytest.mark.asyncio
async def test_rock_response_child_model_with_rock_exception_keeps_result(handle_exc_app):
    """When T inherits from SandboxResponse, result is populated with
    SandboxResponse fields and Pydantic upgrades it to T."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/start_rock_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["code"] == int(codes.BAD_REQUEST)
    assert body["result"] is not None
    sandbox_resp = SandboxResponse(**body["result"])
    assert sandbox_resp.code == codes.BAD_REQUEST
    assert sandbox_resp.failure_reason == "invalid config"


@pytest.mark.asyncio
async def test_rock_response_child_model_with_generic_exception_result_is_none(handle_exc_app):
    """T<:SandboxResponse + generic Exception: result is None, error info
    on envelope only."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/start_generic_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body.get("code") is None
    assert "kaboom" in body["error"]
    assert body["result"] is None


@pytest.mark.asyncio
async def test_handle_exceptions_success_path_unchanged(handle_exc_app):
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Success"
    assert body["result"] == "hello"
    assert body["error"] is None
    assert body.get("code") is None
