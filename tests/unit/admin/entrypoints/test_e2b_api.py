from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints import e2b_api as e2b_api_module
from rock.admin.entrypoints.e2b_api import e2b_router, set_e2b_service
from rock.admin.proto.request import E2BCreateSandboxRequest
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_service import E2BService
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotResultUnknown
from rock.sandbox.snapshot_types import ManagerResultUnknown
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError

_MISSING = object()


@pytest.fixture(autouse=True)
def _restore_e2b_service():
    previous_service = getattr(e2b_api_module, "e2b_service", _MISSING)
    try:
        yield
    finally:
        if previous_service is _MISSING:
            if hasattr(e2b_api_module, "e2b_service"):
                delattr(e2b_api_module, "e2b_service")
        else:
            set_e2b_service(previous_service)


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
    manager.restart_async = AsyncMock()
    manager.extend_timeout = AsyncMock()
    manager.snapshot_runtime_configured = True
    manager.supports_running_delete = False
    template_table = MagicMock()
    template_table.get_ready_template = AsyncMock(
        return_value={
            "image": "linux-dind",
            "cpu_count": 4,
            "memory_mb": 16384,
            "disk_size_mb": 262144,
        }
    )
    set_e2b_service(E2BService(manager, template_table))

    app = FastAPI()
    app.include_router(e2b_router)
    return app, manager, template_table


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}])
async def test_create_sandbox_accepts_official_cli_without_namespace_or_metadata(e2b_app, metadata):
    app, manager, _templates = e2b_app
    body = {"templateID": "linux-dind", "timeout": 600, "secure": True, "allow_internet_access": True}
    if metadata is not None:
        body["metadata"] = metadata
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sandboxes", json=body, headers={"X-API-Key": "e2b-key"})
    assert response.status_code == 201
    config = manager.start_from_template.await_args.args[0]
    assert config.metadata == {}
    assert config.container_name is None
    assert manager.start_from_template.await_args.kwargs["user_info"]["namespace"] == "default"
    assert manager.start_from_template.await_args.kwargs["user_info"]["rock_authorization"] == "Bearer e2b-key"


@pytest.mark.asyncio
async def test_pause_accepts_cli_memory_body_and_rejects_disk_only(e2b_app):
    app, manager, _ = e2b_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/sandboxes/sandbox-123/pause")).status_code == 204
        manager.stop.assert_awaited_once_with("sandbox-123")
        manager.get_status.return_value.state = State.STOPPED
        assert (await client.post("/sandboxes/sandbox-123/pause")).status_code == 409
        manager.stop.assert_awaited_once()
        manager.get_status.return_value.state = State.RUNNING
        assert (await client.post("/sandboxes/sandbox-123/pause", json={"memory": True})).status_code == 204
        assert (await client.post("/sandboxes/sandbox-123/pause", json={"memory": False})).status_code == 400


@pytest.mark.parametrize("action,manager_method", [("pause", "stop"), ("connect", "restart_async")])
@pytest.mark.parametrize("exception,status", [(SnapshotResultUnknown, 503), (SnapshotConflict, 409)])
async def test_pause_resume_preserve_unknown_and_definite_failure_status(
    e2b_app, action, manager_method, exception, status
):
    app, manager, _ = e2b_app
    setattr(manager, manager_method, AsyncMock(side_effect=exception("actor outcome")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"timeout": 600} if action == "connect" else None
        response = await client.post(f"/sandboxes/sandbox-123/{action}", json=body)
    assert response.status_code == status
    assert response.json() == {"code": status, "message": "actor outcome"}


@pytest.mark.parametrize(
    "action,manager_method", [("pause", "stop"), ("connect", "restart_async"), ("status", "get_status")]
)
async def test_lifecycle_routes_translate_manager_unknown_result(e2b_app, action, manager_method):
    app, manager, _ = e2b_app
    error = ManagerResultUnknown("actor outcome")
    setattr(manager, manager_method, AsyncMock(side_effect=error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if action == "status":
            response = await client.get("/sandboxes/sandbox-123")
        else:
            body = {"timeout": 600} if action == "connect" else None
            response = await client.post(f"/sandboxes/sandbox-123/{action}", json=body)
    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "actor outcome"}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/v2"])
@pytest.mark.parametrize("resumed", [False, True])
async def test_connect_routes_resume_same_id_and_return_correct_status(e2b_app, prefix, resumed):
    app, manager, _ = e2b_app
    running = manager.get_status.return_value
    manager.get_status.side_effect = [running.model_copy(update={"state": State.STOPPED}) if resumed else running, running]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"{prefix}/sandboxes/sandbox-123/connect", json={"timeout": 600})
    assert response.status_code == (201 if resumed else 200)
    assert response.json()["sandboxID"] == "sandbox-123"
    manager.restart_async.assert_awaited_once_with("sandbox-123", timeout_seconds=600)


@pytest.mark.parametrize("action", ["pause", "connect"])
@pytest.mark.parametrize("missing", [False, True])
async def test_checkpoint_routes_report_deleted_or_missing_as_not_found(e2b_app, action, missing):
    app, manager, _ = e2b_app
    if missing:
        manager.get_status.side_effect = BadRequestRockError("Sandbox sandbox-123 not found")
    else:
        manager.get_status.return_value.state = State.DELETED
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/sandboxes/sandbox-123/{action}")
    assert response.status_code == 404
    manager.stop.assert_not_awaited()
    manager.restart_async.assert_not_awaited()


@pytest.mark.parametrize("state", [State.RUNNING, State.STOPPED])
async def test_non_snapshot_pause_does_not_stop_generic_sandbox(e2b_app, state):
    app, manager, _ = e2b_app
    manager.snapshot_runtime_configured = False
    manager.get_status.return_value.state = state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sandboxes/sandbox-123/pause")
    assert response.status_code == 409
    manager.stop.assert_not_awaited()


async def test_connect_running_generic_sandbox_extends_timeout_without_restart(e2b_app):
    app, manager, _ = e2b_app
    manager.snapshot_runtime_configured = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sandboxes/sandbox-123/connect", json={"timeout": 37})
    assert response.status_code == 200
    manager.extend_timeout.assert_awaited_once_with("sandbox-123", 37)
    manager.restart_async.assert_not_awaited()


async def test_connect_pending_checkpoint_waits_through_restart_contract(e2b_app):
    app, manager, _ = e2b_app
    running = manager.get_status.return_value
    manager.get_status.side_effect = [running.model_copy(update={"state": State.PENDING}), running]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sandboxes/sandbox-123/connect")
    assert response.status_code == 201
    manager.restart_async.assert_awaited_once_with("sandbox-123", timeout_seconds=300)


def test_default_metadata_is_not_shared_between_requests():
    first = E2BCreateSandboxRequest(templateID="linux-dind", timeout=600)
    second = E2BCreateSandboxRequest(templateID="linux-dind", timeout=600)
    first.metadata["test"] = "first"
    assert second.metadata == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, [], "invalid"])
async def test_create_sandbox_still_rejects_invalid_metadata(e2b_app, metadata):
    app, manager, _ = e2b_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes", json={"templateID": "linux-dind", "timeout": 600, "metadata": metadata}
        )
    assert response.status_code == 400
    assert "body.metadata" in response.json()["message"]
    manager.start_from_template.assert_not_awaited()


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
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "sandboxID": "sandbox-123",
        "envdVersion": "0.3.0",
        "envdAccessToken": "e2b-key",
        "clientID": "rock",
        "templateID": "linux-dind",
    }

    config = manager.start_from_template.await_args.args[0]
    assert config.image == "linux-dind"
    assert config.source_snapshot_id is None
    assert config.auto_clear_time_minutes == 61
    assert config.metadata == request_body["metadata"]
    assert config.env_vars == request_body["envVars"]
    assert config.container_name == "ap-sandbox-123"
    assert config.cpus == 4
    assert config.memory == "16g"
    assert config.disk == "256g"
    template_table.get_ready_template.assert_awaited_once_with(
        "linux-dind",
    )
    assert manager.start_from_template.await_args.kwargs == {
        "user_info": {
            "user_id": "user-123",
            "experiment_id": "experiment-123",
            "namespace": "namespace-123",
            "rock_authorization": "Bearer e2b-key",
        },
        "cluster_info": {"cluster_name": "cluster-123"},
    }


@pytest.mark.asyncio
async def test_create_sandbox_continues_when_template_is_not_ready(e2b_app):
    app, manager, template_table = e2b_app
    template_table.get_ready_template.return_value = None

    with patch("rock.admin.service.e2b_service.logger.info") as log_info:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/sandboxes",
                json={"templateID": "missing", "timeout": 3600, "metadata": {}},
            )

    assert response.status_code == 201
    config = manager.start_from_template.await_args.args[0]
    assert config.image == "missing"
    assert config.cpus == 2
    assert config.memory == "8g"
    assert config.disk == "50G"
    assert config.extended_params.get("use_raw") == "true"
    assert config.template_id is None
    log_info.assert_called_once_with(
        "Template %s is not ready or does not exist; using raw manifest",
        "missing",
    )


@pytest.mark.asyncio
async def test_snapd_create_uses_snapshot_id_and_never_reads_template_table(e2b_app):
    app, manager, template_table = e2b_app
    snapshots = AsyncMock()
    snapshots.get_start_snapshot.return_value = {
        "snapshot_id": "base-snapshot", "kind": "BASE_IMAGE",
        "resource_spec": {"vcpus": 6, "memory_mib": 12288, "disk_size_mb": 102400},
    }
    template_table.get_ready_template.side_effect = AssertionError("snapd must not query TemplateTable")
    set_e2b_service(E2BService(manager, template_table, snapshot_service=snapshots))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sandboxes", json={"templateID": "base-snapshot", "timeout": 3600})
    assert response.status_code == 201
    assert response.json()["templateID"] == "base-snapshot"
    config = manager.start_from_template.await_args.args[0]
    assert config.image == config.source_snapshot_id == "base-snapshot"
    assert config.template_id is None
    assert (config.cpus, config.memory, config.disk) == (6, "12g", "100g")
    template_table.get_ready_template.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,expected_status", [
    ("GET", "/sandboxes/sandbox-123", 200),
    ("POST", "/sandboxes", 201),
])
async def test_legacy_sandbox_responses_do_not_read_snapshot_tokens(e2b_app, method, path, expected_status):
    app, manager, template_table = e2b_app
    manager.snapshot_runtime_configured = False
    manager.get_status.return_value.namespace = "legacy-namespace"
    template_table.get_ready_template.return_value = {
        "image": "legacy-image",
        "cpu_count": 4,
        "memory_mb": 8192,
        "disk_size_mb": 20480,
    }
    sandbox_table = MagicMock()
    sandbox_table.get = AsyncMock(side_effect=RuntimeError("sandbox database is unavailable"))
    set_e2b_service(E2BService(manager, template_table, sandbox_table=sandbox_table))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            json={"templateID": "legacy-image", "timeout": 600} if method == "POST" else None,
            headers={"X-API-Key": "legacy-api-key"},
        )

    assert response.status_code == expected_status
    assert response.json()["sandboxID"] == "sandbox-123"
    sandbox_table.get.assert_not_awaited()
    if method == "POST":
        assert response.json()["envdAccessToken"] == "legacy-api-key"
        manager.start_from_template.assert_awaited_once()
    else:
        assert "envdAccessToken" not in response.json()
        manager.get_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_sandbox_projects_envd_access_token_without_private_authorization(e2b_app):
    app, manager, template_table = e2b_app
    sandbox_table = MagicMock()
    sandbox_table.get = AsyncMock(
        return_value={
            "sandbox_id": "sandbox-123",
            "namespace": "tenant-a",
            "auth_token": "envd-token",
            "rock_authorization_encrypted": "must-not-leak",
        }
    )
    set_e2b_service(
        E2BService(
            manager,
            template_table,
            sandbox_table=sandbox_table,
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": 3600, "metadata": {}},
            headers={"X-Namespace": "tenant-a"},
        )

    assert response.status_code == 201
    assert response.json()["envdAccessToken"] == "envd-token"
    assert "authorization" not in str(response.json()).lower()
    assert "encrypted" not in str(response.json()).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [None, "tenant-a"])
async def test_create_sandbox_namespace_is_metadata_not_access_control(e2b_app, namespace):
    app, manager, template_table = e2b_app
    sandbox_table = MagicMock()
    sandbox_table.get = AsyncMock(
        return_value={
            "sandbox_id": "sandbox-123",
            "namespace": "tenant-b",
            "auth_token": "sandbox-token",
        }
    )
    set_e2b_service(
        E2BService(
            manager,
            template_table,
            sandbox_table=sandbox_table,
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "linux-dind", "timeout": 3600, "metadata": {}},
            headers={} if namespace is None else {"X-Namespace": namespace},
        )

    assert response.status_code == 201
    assert response.json()["envdAccessToken"] == "sandbox-token"


@pytest.mark.asyncio
async def test_create_sandbox_rejects_template_that_is_not_remote_ready(e2b_app):
    app, manager, template_table = e2b_app
    snapshots = AsyncMock()
    snapshots.get_start_snapshot.side_effect = SnapshotConflict("snapshot 'missing' is not REMOTE_READY")
    set_e2b_service(E2BService(manager, template_table, snapshot_service=snapshots))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "missing", "timeout": 3600, "metadata": {}},
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": 409,
        "message": "snapshot 'missing' is not REMOTE_READY",
    }
    manager.start_from_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_sandbox_preserves_snapshot_id_without_template_policy(e2b_app):
    app, manager, template_table = e2b_app
    snapshots = AsyncMock()
    snapshots.get_start_snapshot.return_value = {"snapshot_id": "snapshot-without-policy", "kind": "FULL"}
    set_e2b_service(E2BService(manager, template_table, snapshot_service=snapshots))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"templateID": "snapshot-without-policy", "timeout": 3600, "metadata": {}},
        )

    assert response.status_code == 201
    config = manager.start_from_template.await_args.args[0]
    assert config.image == "snapshot-without-policy"
    assert config.source_snapshot_id == "snapshot-without-policy"
    assert config.cpus == 2
    assert config.memory == "8g"
    assert config.disk == "50G"


@pytest.mark.parametrize("ready_template", [False, True])
async def test_create_sandbox_applies_metadata_overrides_only_on_cold_start(e2b_app, ready_template):
    app, manager, templates = e2b_app
    if not ready_template:
        templates.get_ready_template.return_value = None
    metadata = {"cpuCount": "3", "memoryMB": "12288", "startup_timeout": "120", "custom-key": "value"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes", json={"templateID": "image-or-template", "timeout": 1800, "metadata": metadata}
        )

    assert response.status_code == 201
    config = manager.start_from_template.await_args.args[0]
    assert config.cpus == (4 if ready_template else 3)
    assert config.memory == ("16g" if ready_template else "12g")
    assert config.auto_clear_time_minutes == 30
    assert config.metadata == metadata
    assert manager.start_from_template.await_args.kwargs.get("wait_timeout") == (None if ready_template else 120)


@pytest.mark.parametrize(("key", "value"), [("cpuCount", "nan"), ("memoryMB", "0"), ("startup_timeout", "-1")])
async def test_create_sandbox_rejects_invalid_cold_start_metadata(e2b_app, key, value):
    app, manager, templates = e2b_app
    templates.get_ready_template.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes", json={"templateID": "raw-image", "timeout": 1800, "metadata": {key: value}}
        )

    assert response.status_code == 400
    assert key in response.json()["message"]
    manager.start_from_template.assert_not_awaited()


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
        "envdVersion": "0.3.0",
        "cpuCount": 4,
        "memoryMB": 8192,
        "diskSizeMB": 20480,
        "startedAt": "2026-01-01T07:00:00+08:00",
        "endAt": "2026-01-01T08:00:00+08:00",
    }
    manager.get_status.assert_awaited_once_with("sandbox-123", include_all_states=True, refresh_timeout=False)


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


async def test_snapshot_info_uses_snapshot_id_instead_of_oci_image(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.return_value.source_snapshot_id = "base-snapshot"
    manager.get_status.return_value.image = "registry.example/image:latest"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")
    assert response.status_code == 200
    assert response.json()["templateID"] == "base-snapshot"


async def test_get_stopped_sandbox_maps_to_paused_with_stop_time(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.return_value = manager.get_status.return_value.model_copy(
        update={"state": State.STOPPED, "is_alive": False, "stop_time": "2026-01-01T08:30:00+08:00"}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")
    assert response.status_code == 200
    assert response.json()["state"] == "paused"
    assert response.json()["endAt"] == "2026-01-01T08:30:00+08:00"


@pytest.mark.asyncio
async def test_get_missing_sandbox_returns_404(e2b_app):
    app, manager, _ = e2b_app
    manager.get_status.side_effect = BadRequestRockError("Sandbox missing not found")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Sandbox missing not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [None, "tenant-a"])
async def test_sandbox_lifecycle_routes_keep_id_only_manager_contract(namespace) -> None:
    class LifecycleManager:
        supports_running_delete = False

        def __init__(self) -> None:
            self.calls = []

        async def get_status(self, sandbox_id, *, include_all_states=False, refresh_timeout=True):
            assert refresh_timeout is False
            self.calls.append(("status", sandbox_id, include_all_states))
            return SandboxStatusResponse(
                sandbox_id=sandbox_id,
                state=State.RUNNING,
                is_alive=True,
                image="snapshot-ready",
                metadata={},
                host_ip="127.0.0.1",
                cpus=1,
                memory="128M",
                disk="1G",
                start_time="2026-08-22T18:00:00+08:00",
                auto_stop_time="2026-08-22T19:00:00+08:00",
            )

        async def stop(self, sandbox_id, *, reason="manual"):
            self.calls.append(("stop", sandbox_id))

        async def delete(self, sandbox_id, *, reason="manual"):
            self.calls.append(("delete", sandbox_id))

    manager = LifecycleManager()
    template_table = MagicMock()
    set_e2b_service(E2BService(manager, template_table))  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(e2b_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/sandboxes/snapshot-child",
            headers={} if namespace is None else {"X-Namespace": namespace},
        )
        deleted = await client.delete(
            "/sandboxes/snapshot-child",
            headers={} if namespace is None else {"X-Namespace": namespace},
        )

    assert response.status_code == 200
    assert deleted.status_code == 204
    assert manager.calls == [
        ("status", "snapshot-child", True),
        ("status", "snapshot-child", True),
        ("stop", "snapshot-child"),
        ("delete", "snapshot-child"),
    ]


@pytest.mark.asyncio
async def test_delete_running_sandbox_returns_empty_204(e2b_app):
    app, manager, _ = e2b_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")

    assert response.status_code == 204
    assert response.content == b""
    manager.get_status.assert_awaited_once_with("sandbox-123", include_all_states=True, refresh_timeout=False)
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


@pytest.mark.parametrize("state", [State.STOPPED, State.PENDING])
async def test_kill_stopped_or_pending_snapshot_sandbox_keeps_destruction_path(e2b_app, state):
    app, manager, _ = e2b_app
    manager.supports_running_delete = True
    manager.get_status.return_value.state = state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/sandboxes/sandbox-123")
    assert response.status_code == 204
    if state == State.PENDING:
        manager.stop.assert_awaited_once_with("sandbox-123")
    else:
        manager.stop.assert_not_awaited()
    manager.delete.assert_awaited_once_with("sandbox-123")
    manager.restart_async.assert_not_awaited()


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
