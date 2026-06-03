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
# handle_exceptions: pin down the error envelope shape across response_model
# variants. The decorator must return the same wire shape regardless of the
# endpoint's declared RockResponse[T], so SDK consumers (SandboxResponse(**result))
# stay compatible and RockResponse[str] endpoints don't trip ResponseValidationError.
# ---------------------------------------------------------------------------

from rock._codes import codes  # noqa: E402
from rock.actions import RockResponse, SandboxResponse  # noqa: E402
from rock.common.exception import handle_exceptions  # noqa: E402
from rock.sdk.common.exceptions import BadRequestRockError  # noqa: E402


class _ChildResponse(SandboxResponse):
    """Stand-in for SandboxStartResponse-style models that inherit from
    SandboxResponse and add optional fields. Used to prove handle_exceptions
    does not let Pydantic upgrade the error payload into the child shape
    (which would pollute the response with default-None child fields)."""

    sandbox_id: str | None = None
    host_name: str | None = None


@pytest.fixture
def handle_exc_app():
    app = FastAPI()

    @app.post("/stop_rock_exc")
    @handle_exceptions(error_message="stop sandbox failed")
    async def _stop_rock_exc() -> RockResponse[str]:
        raise BadRequestRockError("bad sandbox id")

    @app.post("/stop_generic_exc")
    @handle_exceptions(error_message="stop sandbox failed")
    async def _stop_generic_exc() -> RockResponse[str]:
        raise RuntimeError("kaboom")

    @app.post("/start_rock_exc")
    @handle_exceptions(error_message="start sandbox failed")
    async def _start_rock_exc() -> RockResponse[_ChildResponse]:
        raise BadRequestRockError("invalid config")

    @app.post("/ok")
    @handle_exceptions()
    async def _ok() -> RockResponse[str]:
        return RockResponse(result="hello")

    return app


@pytest.mark.asyncio
async def test_rock_response_str_with_rock_exception_returns_sandbox_response_result(handle_exc_app):
    """Regression: RockResponse[str] + RockException used to raise
    ResponseValidationError because Pydantic can't coerce SandboxResponse to
    str. The JSONResponse path now bypasses response_model validation."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/stop_rock_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["message"] == "stop sandbox failed"
    assert body["result"] is not None
    # Mirror SDK consumer path: rock/sdk/sandbox/client.py builds SandboxResponse
    # from result and feeds it to raise_for_code.
    sandbox_resp = SandboxResponse(**body["result"])
    assert sandbox_resp.code == codes.BAD_REQUEST
    assert sandbox_resp.failure_reason == "bad sandbox id"


@pytest.mark.asyncio
async def test_rock_response_str_with_generic_exception_returns_none_result(handle_exc_app):
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/stop_generic_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["message"] == "stop sandbox failed"
    assert body["result"] is None
    assert "kaboom" in body["error"]


@pytest.mark.asyncio
async def test_rock_response_child_model_with_rock_exception_no_field_pollution(handle_exc_app):
    """Regression: when T is a subclass of SandboxResponse, the pre-fix code
    let Pydantic upgrade the returned SandboxResponse into the child type,
    populating child-only fields with default None. Result then carried noise
    like {sandbox_id: null, host_name: null}. The fix returns JSONResponse so
    no coercion happens and only SandboxResponse fields appear."""
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/start_rock_exc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Failed"
    assert body["result"] is not None
    assert set(body["result"].keys()) == {"code", "exit_code", "failure_reason"}


@pytest.mark.asyncio
async def test_handle_exceptions_success_path_unchanged(handle_exc_app):
    async with AsyncClient(transport=ASGITransport(app=handle_exc_app), base_url="http://test") as client:
        resp = await client.post("/ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Success"
    assert body["result"] == "hello"
    assert body["error"] is None
