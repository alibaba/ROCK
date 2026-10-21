import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.core.snapshot_table import SnapshotTable
from rock.admin.core.template_table import TemplateTable
from rock.admin.entrypoints import e2b_api
from rock.admin.entrypoints.e2b_api import e2b_router, set_e2b_service
from rock.admin.proto.e2b_snapshot import (
    E2BCreateTemplateResponse,
    E2BForkResult,
    E2BSnapshot,
    E2BTemplate,
    E2BTemplateBuild,
)
from rock.admin.proto.response import E2BCreateSandboxResponse
from rock.admin.service.e2b_service import E2BService
from rock.admin.service.e2b_snapshot_service import E2BSnapshotService
from rock.admin.service.snapshot_manager import SnapshotManager, SnapshotNotFound

NOW = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)


def test_e2b_snapshot_openapi_exposes_no_namespace_or_idempotency_header() -> None:
    app = FastAPI()
    app.include_router(e2b_router)
    openapi = app.openapi()
    snapshot_paths = {
        "/sandboxes/{sandboxID}",
        "/sandboxes/{sandboxID}/pause",
        "/sandboxes/{sandboxID}/connect",
        "/sandboxes/{sandboxID}/snapshots",
        "/sandboxes/{sandboxID}/fork",
        "/snapshots",
        "/templates/{templateID}",
        "/v3/templates",
        "/v2/templates/{templateID}/builds/{buildID}",
    }
    for path in snapshot_paths:
        for operation in openapi["paths"][path].values():
            header_names = {
                parameter["name"] for parameter in operation.get("parameters", []) if parameter["in"] == "header"
            }
            assert "X-Namespace" not in header_names
            assert "Idempotency-Key" not in header_names
    namespace_header = next(
        parameter
        for parameter in openapi["paths"]["/sandboxes"]["post"]["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "X-Namespace"
    )
    assert namespace_header["required"] is False
    assert namespace_header["schema"]["default"] == "default"
    assert "metadata" not in openapi["components"]["schemas"]["E2BCreateSandboxRequest"]["required"]


@pytest.fixture
def snapshot_api_app():
    adapter = MagicMock()
    adapter.list_snapshots = AsyncMock(return_value=([E2BSnapshot(snapshotID="snapshot-1")], "next-page"))
    adapter.get_template = AsyncMock(
        return_value=E2BTemplate(
            templateID="template-1",
            createdAt=NOW,
            updatedAt=NOW,
            builds=[
                E2BTemplateBuild(
                    buildID="template-1",
                    status="ready",
                    createdAt=NOW,
                    updatedAt=NOW,
                    finishedAt=NOW,
                    cpuCount=2,
                    memoryMB=8192,
                    diskSizeMB=51200,
                    envdVersion="0.1.0",
                )
            ],
        )
    )
    adapter.delete_template = AsyncMock()
    adapter.create_snapshot = AsyncMock(return_value=E2BSnapshot(snapshotID="snapshot-created"))
    adapter.fork_sandbox = AsyncMock(
        return_value=[
            E2BForkResult(
                sandbox=E2BCreateSandboxResponse(
                    sandboxID="child-a",
                    envdVersion="0.1.0",
                    clientID="rock",
                    templateID="template-1",
                    envdAccessToken="child-token",
                )
            )
        ]
    )
    adapter.create_template = AsyncMock(
        return_value=E2BCreateTemplateResponse(templateID="template-new", buildID="template-new")
    )
    adapter.start_template_build = AsyncMock()
    service = E2BService(MagicMock(), MagicMock(), snapshot_service=adapter)
    set_e2b_service(service)
    app = FastAPI()
    app.include_router(e2b_router)
    return app, adapter


async def test_list_snapshots_uses_e2b_array_and_next_token_header(snapshot_api_app):
    app, adapter = snapshot_api_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/snapshots?limit=10&nextToken=cursor",
        )

    assert response.status_code == 200
    assert response.json() == [{"snapshotID": "snapshot-1", "names": []}]
    assert response.headers["X-Next-Token"] == "next-page"
    adapter.list_snapshots.assert_awaited_once_with(
        limit=10,
        next_token="cursor",
    )


async def test_get_template_projects_typed_metadata_without_canonical_spec(snapshot_api_app):
    app, _ = snapshot_api_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/templates/template-1",
        )

    assert response.status_code == 200
    assert response.json()["builds"][0]["status"] == "ready"
    assert response.json()["createdAt"] == "2026-08-22T08:00:00Z"
    assert "spec" not in response.json()


async def test_delete_template_is_empty_204(snapshot_api_app):
    app, adapter = snapshot_api_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            "/templates/template-1",
        )

    assert response.status_code == 204
    assert response.content == b""
    adapter.delete_template.assert_awaited_once_with("template-1")


async def test_missing_template_maps_to_404_without_internal_details(snapshot_api_app):
    app, adapter = snapshot_api_app
    adapter.get_template.side_effect = SnapshotNotFound("snapshot 'missing' does not exist")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/templates/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "snapshot 'missing' does not exist"}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_missing_template_uses_real_service_not_found_mapping(db_provider, monkeypatch, method):
    templates = TemplateTable(db_provider)
    manager = SnapshotManager(SnapshotTable(db_provider), MagicMock(), MagicMock())
    manager._sandbox_lifecycle.list_active_snapshot_users = AsyncMock(return_value=[])
    adapter = E2BSnapshotService(manager, MagicMock())
    service = E2BService(MagicMock(), templates, snapshot_service=adapter)
    monkeypatch.setattr(e2b_api, "e2b_service", service, raising=False)
    app = FastAPI()
    app.include_router(e2b_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, "/templates/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "template 'missing' does not exist"}


@pytest.mark.parametrize("metadata", [{}, {"name": "project-template", "alias": "stable", "tags": ["test"]}])
async def test_template_metadata_round_trip_through_real_service_and_database(db_provider, monkeypatch, metadata):
    templates = TemplateTable(db_provider)
    manager = SnapshotManager(SnapshotTable(db_provider), MagicMock(), MagicMock())
    manager._sandbox_lifecycle.list_active_snapshot_users = AsyncMock(return_value=[])
    adapter = E2BSnapshotService(manager, MagicMock())
    service = E2BService(MagicMock(), templates, snapshot_service=adapter)
    monkeypatch.setattr(e2b_api, "e2b_service", service, raising=False)
    app = FastAPI()
    app.include_router(e2b_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v3/templates", json={**metadata, "cpuCount": 3, "memoryMB": 1024})
        assert created.status_code == 202
        template_id = created.json()["templateID"]
        stored = await manager.get_snapshot_record(template_id)
        viewed = await client.get(f"/templates/{template_id}")
        assert viewed.status_code == 200
        assert viewed.json()["names"] == created.json()["names"] == stored["product_metadata"]["names"]
        assert viewed.json()["aliases"] == created.json()["aliases"] == stored["product_metadata"]["aliases"]
        assert viewed.json()["builds"][0]["status"] == "building"
        assert viewed.json()["builds"][0]["cpuCount"] == 3
        assert viewed.json()["builds"][0]["memoryMB"] == 1024
        assert "spec" not in viewed.json()

        deleted = await client.delete(f"/templates/{template_id}")
        assert deleted.status_code == 204
        assert (await manager._snapshots.get(template_id))["state"] == "DELETED"
        for method in ("GET", "DELETE"):
            missing = await client.request(method, f"/templates/{template_id}")
            assert missing.status_code == 404
            assert missing.json() == {"code": 404, "message": f"template {template_id!r} does not exist"}


async def test_create_snapshot_waiting_boundary_uses_e2b_201_projection(snapshot_api_app):
    app, adapter = snapshot_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes/source-a/snapshots",
            json={},
        )
    assert response.status_code == 201
    assert response.json() == {"snapshotID": "snapshot-created", "names": []}
    assert adapter.create_snapshot.await_args.kwargs == {}


async def test_fork_projects_children_and_passes_durable_timeout(snapshot_api_app):
    app, adapter = snapshot_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sandboxes/source-a/fork",
            json={"count": 1, "timeout": 37},
        )
    assert response.status_code == 201
    assert response.json()[0]["sandbox"]["sandboxID"] == "child-a"
    request = adapter.fork_sandbox.await_args.args[1]
    assert request.timeout == 37
    assert adapter.fork_sandbox.await_args.kwargs == {}


async def test_template_create_then_build_routes_keep_one_shared_identity(snapshot_api_app):
    app, adapter = snapshot_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/v3/templates",
            json={"cpuCount": 2, "memoryMB": 8192},
        )
        built = await client.post(
            "/v2/templates/template-new/builds/template-new",
            json={"fromImage": "registry.example/base@sha256:" + "a" * 64},
        )
    assert created.status_code == 202
    assert created.json()["templateID"] == created.json()["buildID"] == "template-new"
    assert built.status_code == 202
    assert adapter.create_template.await_args.kwargs == {}
    adapter.start_template_build.assert_awaited_once()


@pytest.mark.parametrize(
    ("path", "body", "method", "status_code"),
    [
        ("/sandboxes/source-a/snapshots", {}, "create_snapshot", 201),
        ("/sandboxes/source-a/fork", {"count": 1}, "fork_sandbox", 201),
        ("/v3/templates", {}, "create_template", 202),
    ],
)
async def test_old_idempotency_header_is_ignored(snapshot_api_app, path, body, method, status_code):
    app, adapter = snapshot_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(2):
            response = await client.post(path, json=body, headers={"Idempotency-Key": "same-old-key"})
            assert response.status_code == status_code

    service_method = getattr(adapter, method)
    assert service_method.await_count == 2
    assert all(call.kwargs == {} for call in service_method.await_args_list)
