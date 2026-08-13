from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints.e2b_api import e2b_router, set_e2b_service
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_service import E2BService
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError


@pytest.fixture
def e2b_app():
    manager = MagicMock()
    manager.start_from_template = AsyncMock(
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
            host_name="sandbox-123",
            host_ip="10.0.1.23",
            is_alive=True,
            image="linux-dind",
            metadata={
                "ap-job-id": "job-123",
                "ap-template": "swe-bench",
                "e2b.agents.kruise.io/return-sandbox-ip": "true",
            },
            cpus=4,
            memory="8g",
            disk="20g",
            create_time="2026-01-01T06:59:00+08:00",
            start_time="2026-01-01T07:00:00+08:00",
            auto_stop_time="2026-01-01T08:00:00+08:00",
        )
    )
    manager.stop = AsyncMock()
    manager.delete = AsyncMock()
    manager.supports_running_delete = False
    template_table = MagicMock()
    template_table.get_ready_resource_spec = AsyncMock(
        return_value={"cpu_count": 4, "memory_mb": 16384, "disk_size_mb": 262144}
    )
    set_e2b_service(E2BService(manager, template_table))

    app = FastAPI()
    app.include_router(e2b_router)
    return app, manager, template_table


@pytest.mark.asyncio
async def test_create_sandbox_returns_e2b_response_and_maps_request(e2b_app):
    app, manager, template_table = e2b_app
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
        "clientID": "rock",
        "templateID": "linux-dind",
    }

    config = manager.start_from_template.await_args.args[0]
    assert config.image == "linux-dind"
    assert config.auto_clear_time_minutes == 61
    assert config.metadata == request_body["metadata"]
    assert config.env_vars == request_body["envVars"]
    assert config.container_name == "ap-sandbox-123"
    assert config.cpus == 4
    assert config.memory == "16g"
    assert config.disk == "256g"
    template_table.get_ready_resource_spec.assert_awaited_once_with("linux-dind")
    assert manager.start_from_template.await_args.kwargs == {
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
    app, manager, _ = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": timeout, "metadata": {"ap-job-id": "job-123"}},
        )

    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert "timeout" in response.json()["message"]
    manager.start_from_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_sandbox_maps_rock_bad_request_to_http_400(e2b_app):
    app, manager, _ = e2b_app
    manager.start_from_template.side_effect = BadRequestRockError("template is unavailable")

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
    app, manager, _ = e2b_app
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
    manager.start_from_template.assert_not_awaited()


@pytest.mark.parametrize(
    "server_error",
    [RuntimeError("database password leaked"), InternalServerRockError("database password leaked")],
)
@pytest.mark.asyncio
async def test_create_sandbox_hides_server_error(e2b_app, server_error):
    app, manager, _ = e2b_app
    manager.start_from_template.side_effect = server_error

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": 3600, "metadata": {"ap-job-id": "job-123"}},
        )

    assert response.status_code == 500
    assert response.json() == {"code": 500, "message": "Internal server error"}


@pytest.mark.asyncio
async def test_get_sandbox_returns_e2b_detail_from_manager_status(e2b_app):
    app, manager, _ = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")

    assert response.status_code == 200
    assert response.json() == {
        "sandboxID": "sandbox-123",
        "metadata": {
            "ap-job-id": "job-123",
            "ap-template": "swe-bench",
            "e2b.agents.kruise.io/return-sandbox-ip": "true",
            "e2b.agents.kruise.io/sandbox-ip": "10.0.1.23",
        },
        "state": "running",
        "clientID": "rock",
        "templateID": "linux-dind",
        "envdVersion": "0.1.0",
        "cpuCount": 4,
        "memoryMB": 8192,
        "diskSizeMB": 20480,
        "startedAt": "2026-01-01T07:00:00+08:00",
        "endAt": "2026-01-01T08:00:00+08:00",
    }
    manager.get_status.assert_awaited_once_with("sandbox-123", include_all_states=True)


@pytest.mark.asyncio
async def test_get_sandbox_maps_archived_to_paused(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.return_value = SandboxStatusResponse(
        sandbox_id="sandbox-123",
        state=State.ARCHIVED,
        host_name="sandbox-123",
        host_ip="10.0.1.23",
        is_alive=False,
        image="linux-dind",
        metadata={"ap-job-id": "job-123"},
        cpus=4,
        memory="8g",
        disk="20g",
        create_time="2026-01-01T06:59:00+08:00",
        start_time="2026-01-01T07:00:00+08:00",
        archive_time="2026-01-01T09:00:00+08:00",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")

    assert response.status_code == 200
    assert response.json()["state"] == "paused"
    assert response.json()["endAt"] == "2026-01-01T09:00:00+08:00"


@pytest.mark.asyncio
async def test_get_missing_sandbox_returns_404(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.side_effect = BadRequestRockError("Sandbox missing not found")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Sandbox missing not found"}


@pytest.mark.asyncio
async def test_delete_running_sandbox_returns_empty_204(e2b_app):
    app, manager, _ = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")

    assert response.status_code == 204
    assert response.content == b""
    manager.get_status.assert_awaited_once_with("sandbox-123", include_all_states=True)
    manager.stop.assert_awaited_once_with("sandbox-123")
    manager.delete.assert_awaited_once_with("sandbox-123")


@pytest.mark.asyncio
async def test_delete_running_sandbox_uses_direct_delete_when_supported(e2b_app):
    app, manager, _ = e2b_app
    manager.supports_running_delete = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")

    assert response.status_code == 204
    assert response.content == b""
    manager.stop.assert_not_awaited()
    manager.delete.assert_awaited_once_with("sandbox-123")


@pytest.mark.asyncio
async def test_delete_missing_sandbox_returns_404(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.side_effect = BadRequestRockError("Sandbox missing not found")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Sandbox missing not found"}
    manager.stop.assert_not_awaited()
    manager.delete.assert_not_awaited()
