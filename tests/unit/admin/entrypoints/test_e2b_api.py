from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints.e2b_api import e2b_router, set_e2b_sandbox_manager
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError


@pytest.fixture
def e2b_app():
    manager = MagicMock()
    manager.start = AsyncMock(
        return_value=SandboxStartResponse(
            sandbox_id="sandbox-123",
            host_name="sandbox-123",
            host_ip="10.0.1.23",
        )
    )
    manager.get_status = AsyncMock(
        return_value=SandboxStatusResponse(
            sandbox_id="sandbox-123",
            state=State.RUNNING,
            is_alive=True,
        )
    )
    manager.stop = AsyncMock()
    manager.delete = AsyncMock()
    manager.supports_running_delete = False
    set_e2b_sandbox_manager(manager)

    app = FastAPI()
    app.include_router(e2b_router)
    return app, manager


@pytest.mark.asyncio
async def test_create_sandbox_returns_e2b_response_and_maps_request(e2b_app):
    app, manager = e2b_app
    request_body = {
        "templateID": "linux-dind",
        "timeout": 3601,
        "metadata": {
            "ap-sandbox-id": "ap-sandbox-123",
            "ap-job-id": "job-123",
            "ap-template": "swe-bench",
            "e2b.agents.kruise.io/return-sandbox-ip": "true",
        },
        "secure": True,
        "allow_internet_access": True,
        "envVars": {"WORKSPACE": "/workspace"},
        "autoPause": False,
        "autoResume": {},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json=request_body,
            headers={
                "X-User-Id": "user-123",
                "X-Experiment-Id": "experiment-123",
                "X-Namespace": "namespace-123",
                "X-Cluster": "cluster-123",
                "X-Key": "legacy-key",
                "X-API-Key": "e2b-key",
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "sandboxID": "sandbox-123",
        "envdVersion": "0.1.0",
        "clientID": "ap-sandbox",
        "templateID": "linux-dind",
    }

    config = manager.start.await_args.args[0]
    assert config.image == "linux-dind"
    assert config.auto_clear_time_minutes == 61
    assert config.metadata == request_body["metadata"]
    assert config.env_vars == request_body["envVars"]
    assert config.container_name == "ap-sandbox-123"
    assert manager.start.await_args.kwargs == {
        "user_info": {
            "user_id": "user-123",
            "experiment_id": "experiment-123",
            "namespace": "namespace-123",
            "rock_authorization": "Bearer e2b-key",
        },
        "cluster_info": {"cluster_name": "cluster-123"},
    }


@pytest.mark.parametrize("timeout", [0, True, "3600"])
@pytest.mark.asyncio
async def test_create_sandbox_returns_400_for_invalid_timeout(e2b_app, timeout):
    app, manager = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": timeout, "metadata": {"ap-job-id": "job-123"}},
        )

    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert "timeout" in response.json()["message"]
    manager.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_sandbox_maps_rock_bad_request_to_http_400(e2b_app):
    app, manager = e2b_app
    manager.start.side_effect = BadRequestRockError("template is unavailable")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "missing", "timeout": 3600, "metadata": {"ap-job-id": "job-123"}},
        )

    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": "template is unavailable"}


@pytest.mark.parametrize("unsupported_field", ["mcp", "network", "volumeMounts"])
@pytest.mark.asyncio
async def test_create_sandbox_rejects_unsupported_phase_one_fields(e2b_app, unsupported_field):
    app, manager = e2b_app
    body = {
        "templateID": "linux-dind",
        "timeout": 3600,
        "metadata": {"ap-job-id": "job-123"},
        unsupported_field: {},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json=body,
        )

    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert unsupported_field in response.json()["message"]
    manager.start.assert_not_awaited()


@pytest.mark.parametrize(
    "server_error",
    [RuntimeError("database password leaked"), InternalServerRockError("database password leaked")],
)
@pytest.mark.asyncio
async def test_create_sandbox_hides_server_error(e2b_app, server_error):
    app, manager = e2b_app
    manager.start.side_effect = server_error

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": 3600, "metadata": {"ap-job-id": "job-123"}},
        )

    assert response.status_code == 500
    assert response.json() == {"code": 500, "message": "Internal server error"}


@pytest.mark.asyncio
async def test_delete_running_sandbox_returns_empty_204(e2b_app):
    app, manager = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")

    assert response.status_code == 204
    assert response.content == b""
    manager.get_status.assert_awaited_once_with("sandbox-123", include_all_states=True)
    manager.stop.assert_awaited_once_with("sandbox-123")
    manager.delete.assert_awaited_once_with("sandbox-123")


@pytest.mark.asyncio
async def test_delete_running_sandbox_uses_direct_delete_when_supported(e2b_app):
    app, manager = e2b_app
    manager.supports_running_delete = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")

    assert response.status_code == 204
    assert response.content == b""
    manager.stop.assert_not_awaited()
    manager.delete.assert_awaited_once_with("sandbox-123")


@pytest.mark.asyncio
async def test_delete_missing_sandbox_returns_404(e2b_app):
    app, manager = e2b_app
    manager.get_status.side_effect = BadRequestRockError("Sandbox missing not found")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Sandbox missing not found"}
    manager.stop.assert_not_awaited()
    manager.delete.assert_not_awaited()
