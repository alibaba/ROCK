import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from rock.actions import RockResponse
from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.entrypoints.snapshot_api import (
    set_snapshot_manager,
    snapshot_router,
)
from rock.admin.proto.snapshot import (
    CreateBaseImageRequest,
    CreateSnapshotRequest,
    ForkSnapshotRequest,
    ForkSnapshotResponse,
    SnapshotResponse,
    StartFromSnapshotRequest,
    StartFromSnapshotResponse,
)
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotNotFound, SnapshotResultUnknown
from rock.admin.snapd.protocol import ForkChildResult, ForkSandboxRequest
from rock.common.exception import request_validation_exception_handler


@pytest.fixture(autouse=True)
def _reset_snapshot_route_services():
    set_snapshot_manager(None)
    yield
    set_snapshot_manager(None)


def test_public_request_renames_preserve_json_wire() -> None:
    requests_and_wire = [
        (
            CreateBaseImageRequest(snapshot_id="base-a", oci_ref="registry.example/base@sha256:" + "a" * 64),
            {
                "snapshot_id": "base-a",
                "oci_ref": "registry.example/base@sha256:" + "a" * 64,
                "platform": "linux/amd64",
            },
        ),
        (
            CreateSnapshotRequest(snapshot_id="snapshot-a"),
            {"snapshot_id": "snapshot-a"},
        ),
        (
            ForkSnapshotRequest(
                snapshot_id="fork-a",
                count=2,
                target_sandbox_ids=["child-a", "child-b"],
                resource_spec={"vcpus": 2, "memory_mib": 128},
            ),
            {
                "snapshot_id": "fork-a",

                "count": 2,
                "target_sandbox_ids": ["child-a", "child-b"],
                "resource_spec": {"vcpus": 2, "memory_mib": 128},
            },
        ),
        (
            StartFromSnapshotRequest(
                sandbox_id="sandbox-a",
                resource_spec={"vcpus": 1, "memory_mib": 128},
            ),
            {
                "sandbox_id": "sandbox-a",

                "resource_spec": {"vcpus": 1, "memory_mib": 128},
            },
        ),
    ]

    for request, expected_wire in requests_and_wire:
        assert request.model_dump(mode="json") == expected_wire


@pytest.mark.parametrize(
    "request_type,body",
    [
        (CreateBaseImageRequest, {"oci_ref": "registry.example/base", "unknown": True}),
        (CreateSnapshotRequest, {"unknown": True}),
        (ForkSnapshotRequest, {"unknown": True}),
        (CreateSnapshotRequest, {"generation": 1}),
        (CreateSnapshotRequest, {"runtime_epoch": 1}),
        (ForkSnapshotRequest, {"runtime_epoch": 1}),
        (StartFromSnapshotRequest, {"runtime_epoch": 1}),
        (ForkSnapshotRequest, {"generation": 1}),
        (ForkSnapshotRequest, {"failure_policy": "best_effort"}),
        (ForkSnapshotRequest, {"failure_policy": "all_or_nothing"}),
        (StartFromSnapshotRequest, {"unknown": True}),
    ],
)
def test_public_snapshot_requests_still_forbid_unknown_fields(request_type, body) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        request_type.model_validate(body)


@pytest.mark.parametrize("policy", ["all_or_nothing", "best_effort"])
def test_snapd_fork_wire_request_rejects_removed_failure_policy(policy) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ForkSandboxRequest.model_validate({
            "snapshot_id": "snapshot-a",

            "children": [{"sandbox_id": "child-a"}],
            "failure_policy": policy,
        })


def test_snapd_fork_wire_child_accepts_pending_without_guest_observation() -> None:
    child = ForkChildResult(sandbox_id="child-a", state="PENDING", error="child launch reply lost")

    assert child.state == "PENDING"
    assert child.error == "child launch reply lost"
    assert child.guest_endpoint is None


@pytest.mark.parametrize("state", ["PENDING", "FAILED"])
def test_snapd_fork_wire_non_running_child_rejects_guest_observation(state) -> None:
    with pytest.raises(ValidationError, match="non-running fork child must not expose"):
        ForkChildResult(sandbox_id="child-a", state=state, vmm_pid=123)


def test_snapshot_response_projection_and_envelope_preserve_json_wire() -> None:
    created_at = datetime.datetime(2026, 8, 25, 8, 0, tzinfo=datetime.timezone.utc)
    response = SnapshotResponse.from_record(
        {
            "snapshot_id": "snapshot-a",
            "kind": "FULL",
            "state": "REMOTE_READY",
            "source_sandbox_id": "source-a",
            "format_version": 1,
            "created_at": created_at,
            "remote_ready_at": created_at,
            "deleted_at": None,
            "resource_spec": {"memory_bytes": 134217728, "rootfs_bytes": 67108864},
            "spec": {
                "backend_payload": {"value": "must-not-be-projected", "nested": [None, 1]},
            },
        }
    )

    assert isinstance(response, SnapshotResponse)
    assert RockResponse(result=response).model_dump(mode="json")["result"] == {
        "snapshot_id": "snapshot-a",
        "kind": "FULL",
        "state": "REMOTE_READY",
        "status": "ready",
        "source_sandbox_id": "source-a",
        "format_version": 1,
        "memory": {"logical_bytes": 134217728},
        "rootfs": {"logical_bytes": 67108864},
        "created_at": "2026-08-25T08:00:00Z",
        "remote_ready_at": "2026-08-25T08:00:00Z",
        "deleted_at": None,
    }

    fork = ForkSnapshotResponse(snapshot=response, sandbox_ids=["child-a"], children=[{"state": "RUNNING"}])
    assert fork.model_dump(mode="json")["snapshot"] == response.model_dump(mode="json")
    assert StartFromSnapshotResponse(
        sandbox_id="sandbox-a", source_snapshot_id="snapshot-a", state="RUNNING"
    ).model_dump(mode="json") == {
        "sandbox_id": "sandbox-a",
        "source_snapshot_id": "snapshot-a",
        "state": "RUNNING",
    }


def test_openapi_uses_request_suffix_and_exposes_no_legacy_components() -> None:
    app = FastAPI()
    app.include_router(snapshot_router)
    app.include_router(sandbox_router)
    openapi = app.openapi()
    assert "/sandboxes/{sandbox_id}/pause" not in openapi["paths"]
    assert "/sandboxes/{sandbox_id}/resume" not in openapi["paths"]
    assert "/stop" in openapi["paths"]
    assert "/restart" in openapi["paths"]
    request_schemas = {
        operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        for path, methods in openapi["paths"].items()
        if path.startswith("/snapshots") or path in {"/sandboxes/{source_id}/snapshots", "/sandboxes/{source_id}/fork"}
        for operation in methods.values()
        if "requestBody" in operation
    }

    assert request_schemas == {
        "CreateBaseImageRequest",
        "CreateSnapshotRequest",
        "ForkSnapshotRequest",
        "StartFromSnapshotRequest",
    }
    snapshot_paths = {
        path: methods
        for path, methods in openapi["paths"].items()
        if path.startswith("/snapshots") or path in {"/sandboxes/{source_id}/snapshots", "/sandboxes/{source_id}/fork"}
    }
    for methods in snapshot_paths.values():
        for operation in methods.values():
            header_names = {
                parameter["name"] for parameter in operation.get("parameters", []) if parameter["in"] == "header"
            }
            assert "X-Namespace" not in header_names
            assert "Idempotency-Key" not in header_names


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_native_checkpoint_workflows_do_not_add_pause_or_resume_routes(action):
    app = FastAPI()
    app.include_router(sandbox_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/sandboxes/sandbox-a/{action}")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "ignored-old-key"}])
async def test_all_canonical_routes_keep_their_existing_json_wire(headers) -> None:
    created_at = datetime.datetime(2026, 8, 25, 8, 0, tzinfo=datetime.timezone.utc)
    record = {
        "snapshot_id": "snapshot-a",
        "kind": "FULL",
        "state": "REMOTE_READY",
        "source_sandbox_id": "source-a",
        "format_version": 1,
        "created_at": created_at,
        "remote_ready_at": created_at,
        "deleted_at": None,
        "resource_spec": {"memory_bytes": 134217728, "rootfs_bytes": 67108864},
        "spec": {"backend_v2": {"private": "opaque"}},
    }
    manager = MagicMock()
    manager.create_base_image = AsyncMock(return_value=record)
    manager.start_async = AsyncMock(
        return_value={"sandbox_id": "sandbox-a", "source_snapshot_id": "snapshot-a", "state": "RUNNING"}
    )
    manager.delete_snapshot = AsyncMock(return_value={**record, "state": "DELETED", "deleted_at": created_at})
    manager.get_snapshot_record = AsyncMock(return_value=record)
    manager.list_snapshot_records = AsyncMock(return_value=[record])
    sandbox_manager = MagicMock()
    sandbox_manager.snapshot_sandbox = AsyncMock(return_value=record)
    sandbox_manager.fork_sandbox = AsyncMock(return_value=(record, [{"sandbox_id": "child-a", "state": "RUNNING"}]))
    set_snapshot_manager(manager)
    set_sandbox_manager(sandbox_manager)
    app = FastAPI()
    app.include_router(snapshot_router)
    app.include_router(sandbox_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = [
            await client.post(
                "/snapshots/base-images",
                json={"snapshot_id": "base-a", "oci_ref": "registry.example/base"},
                headers=headers,
            ),
            await client.post(
                "/sandboxes/source-a/snapshots",
                json={"snapshot_id": "snapshot-a"},
                headers=headers,
            ),
            await client.post(
                "/sandboxes/source-a/fork",
                json={"snapshot_id": "snapshot-a", "count": 1, "target_sandbox_ids": ["child-a"]},
                headers=headers,
            ),
            await client.post(
                "/snapshots/snapshot-a/start_async",
                json={"sandbox_id": "sandbox-a", "resource_spec": {"vcpus": 1, "memory_mib": 128}},
                headers=headers,
            ),
            await client.get("/snapshots/snapshot-a"),
            await client.get("/snapshots"),
            await client.delete("/snapshots/snapshot-a"),
        ]

    assert [response.status_code for response in responses] == [200] * 7
    assert responses[0].json()["result"]["snapshot_id"] == "snapshot-a"
    assert "template_id" not in responses[0].json()["result"]
    assert responses[1].json()["result"]["status"] == "ready"
    assert responses[2].json()["result"]["sandbox_ids"] == ["child-a"]
    assert responses[3].json()["result"] == {
        "sandbox_id": "sandbox-a",
        "source_snapshot_id": "snapshot-a",
        "state": "RUNNING",
    }
    assert responses[4].json()["result"]["memory"] == {"logical_bytes": 134217728}
    assert "template_id" not in responses[4].json()["result"]
    assert len(responses[5].json()["result"]) == 1
    assert responses[6].json()["result"]["status"] == "deleted"
    assert isinstance(manager.create_base_image.await_args.args[0], CreateBaseImageRequest)
    assert isinstance(sandbox_manager.snapshot_sandbox.await_args.args[1], CreateSnapshotRequest)
    assert isinstance(sandbox_manager.fork_sandbox.await_args.args[1], ForkSnapshotRequest)
    assert isinstance(manager.start_async.await_args.args[1], StartFromSnapshotRequest)
    for method in (
        manager.create_base_image,
        sandbox_manager.snapshot_sandbox,
        sandbox_manager.fork_sandbox,
        manager.start_async,
    ):
        assert method.await_args.kwargs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path, method", [
    ("/sandboxes/source-a/snapshots", "snapshot_sandbox"),
    ("/sandboxes/source-a/fork", "fork_sandbox"),
    ("/snapshots/snapshot-a/start_async", "start_async"),
])
@pytest.mark.parametrize("error", [
    SnapshotConflict("snapshot ID already exists"),
    SnapshotConflict("source sandbox is busy; request was not submitted"),
    SnapshotResultUnknown("lost snapd reply"),
])
async def test_creation_conflicts_and_unknown_results_use_native_failure_envelope(path, method, error):
    manager = MagicMock()
    operation = AsyncMock(side_effect=error)
    setattr(manager, method, operation)
    set_snapshot_manager(manager)
    set_sandbox_manager(manager)
    app = FastAPI()
    app.include_router(snapshot_router)
    app.include_router(sandbox_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json={})

    assert response.status_code == 200
    assert response.json()["status"] == "Failed"
    assert response.json()["error"] == str(error)
    assert "detail" not in response.json()
    operation.assert_awaited_once()


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/snapshots/base-images", {"oci_ref": "registry.example/base"}),
    ("POST", "/snapshots/snapshot-a/start_async", {}),
    ("GET", "/snapshots/snapshot-a", None),
    ("GET", "/snapshots", None),
    ("DELETE", "/snapshots/snapshot-a", None),
])
async def test_unconfigured_snapshot_service_returns_native_failure(method, path, body):
    app = FastAPI()
    app.include_router(snapshot_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "Failed"
    assert response.json()["error"] == "snapshot service is not ready"
    assert response.json()["result"]["code"] == 5000


@pytest.mark.parametrize("method,path,body,error_fragment", [
    ("POST", "/snapshots/base-images", {}, "oci_ref"),
    ("POST", "/sandboxes/source-a/snapshots", {"unknown": True}, "unknown"),
    ("POST", "/sandboxes/source-a/fork", {"count": 0}, "count"),
    ("POST", "/snapshots/snapshot-a/start_async", {"unknown": True}, "unknown"),
    ("GET", "/snapshots?limit=0", None, "limit"),
])
async def test_snapshot_validation_uses_same_native_failure_envelope(method, path, body, error_fragment):
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.include_router(snapshot_router)
    app.include_router(sandbox_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "Failed"
    assert error_fragment in response.json()["error"]
    assert "detail" not in response.json()


@pytest.mark.parametrize("error", [SnapshotNotFound("snapshot 'missing' does not exist"), RuntimeError("database unavailable")])
async def test_snapshot_lookup_failures_keep_the_native_envelope(error):
    manager = MagicMock()
    manager.get_snapshot_record = AsyncMock(side_effect=error)
    set_snapshot_manager(manager)
    app = FastAPI()
    app.include_router(snapshot_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/snapshots/missing")
    assert response.status_code == 200
    assert response.json()["status"] == "Failed"
    assert response.json()["error"] == str(error)
