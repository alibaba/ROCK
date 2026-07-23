"""
Route-level tests for GET /get_status on sandbox_proxy_router.

Uses a mocked SandboxProxyService injected via set_sandbox_proxy_service.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.proto.response import SandboxStatusResponse
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def mock_service():
    svc = MagicMock(spec=SandboxProxyService)
    svc.get_status = AsyncMock()
    set_sandbox_proxy_service(svc)
    return svc


@pytest.fixture
def app(mock_service):
    a = FastAPI()
    a.include_router(sandbox_proxy_router)
    return a


@pytest.fixture
def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestGetStatusRoute:
    async def test_get_status_success(self, client, mock_service):
        mock_service.get_status.return_value = SandboxStatusResponse(
            sandbox_id="sandbox-1",
            state=State.RUNNING,
            is_alive=True,
            host_ip="10.0.0.1",
            num_gpus=2,
            accelerator_type="H20",
        )

        resp = await client.get("/get_status", params={"sandbox_id": "sandbox-1"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["result"]["sandbox_id"] == "sandbox-1"
        assert body["result"]["state"] == State.RUNNING
        assert body["result"]["is_alive"] is True
        assert body["result"]["num_gpus"] == 2
        assert body["result"]["accelerator_type"] == "H20"
        mock_service.get_status.assert_awaited_once_with("sandbox-1", False)

    async def test_get_status_passes_include_all_states(self, client, mock_service):
        mock_service.get_status.return_value = SandboxStatusResponse(
            sandbox_id="sandbox-1", state=State.STOPPED, is_alive=False
        )

        resp = await client.get("/get_status", params={"sandbox_id": "sandbox-1", "include_all_states": True})

        assert resp.status_code == 200
        mock_service.get_status.assert_awaited_once_with("sandbox-1", True)

    async def test_get_status_not_found_returns_failed(self, client, mock_service):
        mock_service.get_status.side_effect = BadRequestRockError("Sandbox sandbox-1 not found")

        resp = await client.get("/get_status", params={"sandbox_id": "sandbox-1"})

        body = resp.json()
        assert body["status"] == "Failed"

    async def test_get_status_missing_sandbox_id_returns_422(self, client, mock_service):
        resp = await client.get("/get_status")
        assert resp.status_code == 422
