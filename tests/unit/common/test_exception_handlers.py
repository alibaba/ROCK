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
