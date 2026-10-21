import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.proto.e2b_snapshot import (
    E2BCreateSnapshotRequest,
    E2BCreateTemplateRequest,
    E2BForkRequest,
    E2BStartTemplateBuildRequest,
)
from rock.admin.service.e2b_snapshot_service import E2BSnapshotService
from rock.admin.service.snapshot_manager import SnapshotNotFound

NOW = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)


def _record(snapshot_id: str, *, state: str = "REMOTE_READY"):
    return {
        "snapshot_id": snapshot_id,
        "kind": "BASE_IMAGE",
        "state": state,
        "format_version": 1,
        "spec": {"backend_payload": {"value": "must-never-leak"}},
        "created_at": NOW,
        "remote_ready_at": NOW if state == "REMOTE_READY" else None,
    }


@pytest.fixture
def snapshot_adapter():
    snapshot_manager = MagicMock()
    snapshot_manager.get_snapshot_record = AsyncMock()
    snapshot_manager.list_snapshot_records = AsyncMock()
    snapshot_manager.delete_snapshot = AsyncMock()
    snapshot_manager.create_base_image = AsyncMock()
    snapshot_manager.create_base_image_draft = AsyncMock()
    sandbox_manager = MagicMock()
    sandbox_manager.snapshot_sandbox = AsyncMock()
    sandbox_manager.fork_sandbox = AsyncMock()
    sandbox_manager.get_product_view = AsyncMock()
    return (
        E2BSnapshotService(snapshot_manager, sandbox_manager),
        snapshot_manager,
        sandbox_manager,
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("CREATING", "building"),
        ("LOCAL_READY", "building"),
        ("REMOTE_READY", "ready"),
        ("FAILED", "error"),
    ],
)
async def test_template_status_comes_only_from_canonical_snapshot(snapshot_adapter, state, expected):
    adapter, snapshot_manager, _ = snapshot_adapter
    snapshot_manager.get_snapshot_record.return_value = _record("template-1", state=state)
    snapshot_manager.get_snapshot_record.return_value["product_metadata"] = {
        "names": ["public-name"],
        "aliases": ["stable"],
        "private": "must-not-be-projected",
    }
    snapshot_manager.get_snapshot_record.return_value["resource_spec"] = {
        "vcpus": 8,
        "memory_mib": 16384,
        "disk_size_mb": 102400,
        "fiber_pool_id": "private-placement-policy",
    }

    result = await adapter.get_template("template-1")
    wire = result.model_dump(by_alias=True, exclude_none=True, mode="json")

    assert wire["builds"][0]["status"] == expected
    assert wire["builds"][0]["buildID"] == "template-1"
    assert wire["builds"][0]["cpuCount"] == 8
    assert wire["names"] == ["public-name"]
    assert wire["aliases"] == ["stable"]
    assert "spec" not in wire
    assert "must-not-be-projected" not in str(wire)
    assert "must-never-leak" not in str(wire)
    assert "fiber" not in str(wire).lower()


async def test_list_snapshots_projects_raw_e2b_items_and_opaque_cursor(snapshot_adapter):
    adapter, snapshot_manager, _ = snapshot_adapter
    snapshot_manager.list_snapshot_records.side_effect = [
        [_record("snapshot-3"), _record("snapshot-2"), _record("snapshot-1")],
        [_record("snapshot-1")],
    ]

    first, cursor = await adapter.list_snapshots(limit=2)
    second, final_cursor = await adapter.list_snapshots(
        limit=2,
        next_token=cursor,
    )

    assert [item.model_dump(by_alias=True) for item in first] == [
        {"snapshotID": "snapshot-3", "names": []},
        {"snapshotID": "snapshot-2", "names": []},
    ]
    assert [item.snapshot_id for item in second] == ["snapshot-1"]
    assert final_cursor is None
    assert snapshot_manager.list_snapshot_records.await_args_list[1].kwargs["offset"] == 2


async def test_list_snapshots_rejects_malformed_cursor(snapshot_adapter):
    adapter, _, _ = snapshot_adapter

    with pytest.raises(ValueError, match="nextToken is invalid"):
        await adapter.list_snapshots(next_token="not-a-rock-cursor")


async def test_deleted_snapshot_is_not_exposed(snapshot_adapter):
    adapter, snapshot_manager, _ = snapshot_adapter
    snapshot_manager.get_snapshot_record.side_effect = SnapshotNotFound("not found")

    with pytest.raises(SnapshotNotFound):
        await adapter.get_template("private-template")


async def test_template_delete_soft_deletes_its_snapshot(snapshot_adapter):
    adapter, snapshot_manager, _ = snapshot_adapter

    snapshot_manager.get_snapshot_record.return_value = _record("template-1")
    await adapter.delete_template("template-1")

    snapshot_manager.delete_snapshot.assert_awaited_once_with("template-1")


async def test_template_build_uses_allocated_snapshot_identity(snapshot_adapter):
    adapter, snapshot_manager, _ = snapshot_adapter
    record = _record("base-allocated", state="CREATING")
    record["product_metadata"] = {}
    snapshot_manager.get_snapshot_record.return_value = record
    await adapter.start_template_build(
        "base-allocated", "base-allocated",
        E2BStartTemplateBuildRequest(fromImage="registry.example/image:alias"),
    )
    request = snapshot_manager.create_base_image.await_args.args[0]
    assert request.snapshot_id == "base-allocated"
    assert request.oci_ref == "registry.example/image:alias"


async def test_create_template_assigns_a_fresh_random_id_each_time(snapshot_adapter):
    adapter, snapshot_manager, _ = snapshot_adapter
    request = E2BCreateTemplateRequest(name="example", tags=["test"])

    first = await adapter.create_template(request)
    second = await adapter.create_template(request)

    assert first.template_id != second.template_id
    for result, call in zip((first, second), snapshot_manager.create_base_image_draft.await_args_list, strict=True):
        assert result.template_id.startswith("base-")
        suffix = result.template_id.removeprefix("base-")
        assert uuid.UUID(suffix).version == 4
        assert uuid.UUID(suffix).hex == suffix
        assert result.template_id == result.build_id == call.kwargs["snapshot_id"]
        assert result.names == ["example"]
        assert result.tags == ["test"]


async def test_create_snapshot_delegates_without_an_idempotency_key(snapshot_adapter):
    adapter, snapshot_manager, sandbox_manager = snapshot_adapter
    sandbox_manager.snapshot_sandbox.return_value = _record("snap-new")
    snapshot_manager.get_snapshot_record.return_value = _record("snap-new")

    result = await adapter.create_snapshot("source-a", E2BCreateSnapshotRequest())

    assert result.snapshot_id == "snap-new"
    call = sandbox_manager.snapshot_sandbox.await_args
    assert call.args[0] == "source-a"
    assert call.args[1].snapshot_id is None
    assert call.kwargs == {}


async def test_fork_maps_timeout_and_delegates_source_geometry_to_canonical_manager(snapshot_adapter):
    adapter, _, sandbox_manager = snapshot_adapter
    sandbox_manager.fork_sandbox.return_value = (
        _record("fork-snapshot"),
        [{"sandbox_id": "child-a", "state": "RUNNING"}],
    )
    sandbox_manager.get_product_view.side_effect = [
        MagicMock(source_snapshot_id="fork-snapshot", image="snapshot:fork-snapshot", auth_token="child-token"),
    ]

    result = await adapter.fork_sandbox(
        "source-a",
        E2BForkRequest(timeout=37, count=1),
    )

    call = sandbox_manager.fork_sandbox.await_args
    assert call.kwargs == {"child_timeout_seconds": 37}
    # E2B has no resource fields.  An empty override tells the canonical
    # manager to inherit the source sandbox's authoritative geometry.
    assert call.args[1].resource_spec == {}
    assert "failure_policy" not in call.args[1].model_dump()
    assert result[0].sandbox is not None
    assert result[0].sandbox.template_id == "fork-snapshot"
    assert result[0].sandbox.envd_access_token == "child-token"


@pytest.mark.parametrize("state,code", [("FAILED", 500), ("PENDING", 503)])
@pytest.mark.parametrize("message", [None, "child launch could not be confirmed"])
async def test_fork_preserves_success_and_per_child_failure_semantics(snapshot_adapter, state, code, message):
    adapter, _, sandbox_manager = snapshot_adapter
    sandbox_manager.fork_sandbox.return_value = (
        _record("fork-snapshot"),
        [
            {"sandbox_id": "child-a", "state": "RUNNING"},
            {"sandbox_id": "child-b", "state": state, "last_error": message},
        ],
    )
    sandbox_manager.get_product_view.side_effect = [
        MagicMock(source_snapshot_id="fork-snapshot", image="template-a", auth_token="child-token"),
    ]

    result = await adapter.fork_sandbox("source-a", E2BForkRequest(count=2))

    assert result[0].sandbox.sandbox_id == "child-a"
    assert result[0].sandbox.envd_access_token == "child-token"
    assert result[0].error is None
    assert result[1].sandbox is None
    assert result[1].error.code == code
    assert result[1].error.message == (
        message or ("fork child result is unknown" if state == "PENDING" else "fork child failed")
    )
    sandbox_manager.get_product_view.assert_awaited_once_with("child-a")
