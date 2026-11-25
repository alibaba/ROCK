from __future__ import annotations

import asyncio

import pytest

from rock.admin.proto.snapshot import StartFromSnapshotRequest
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotManager, SnapshotNotFound, SnapshotReconciler
from rock.sandbox.snapshot_types import ActiveSnapshotUser, SandboxProductView


class _Catalog:
    def __init__(self) -> None:
        self.records = {
            "snapshot-a": {
                "snapshot_id": "snapshot-a",
                "kind": "FULL",
                "format_version": 1,
                "state": "REMOTE_READY",
                "spec": {"backend_payload": {"value": "snapshot-a"}},
                "deleted_at": None,
                "resource_spec": {"vcpus": 1, "memory_mib": 256},
                "source_sandbox_id": "source",
                "source_worker_ip": "10.0.0.1",
            }
        }
        self.fail_delete = False

    async def get(self, snapshot_id):
        row = self.records.get(snapshot_id)
        return None if row is None else dict(row)

    async def conditional_transition(
        self,
        snapshot_id,
        *,
        expected_state,
        new_state,
        values=None,
        acceptable_replay_states=(),
        replay_identity_values=None,
    ):
        del acceptable_replay_states, replay_identity_values
        if self.fail_delete:
            raise RuntimeError("injected catalog failure")
        row = self.records[snapshot_id]
        assert row["state"] == expected_state
        row["state"] = new_state
        row.update(values or {})
        return dict(row)

    async def list_by_state(self, state, *, limit, after=None):
        del state, limit, after
        return []

    async def list(self, *, include_deleted=False, offset=0, limit=100, **kwargs):
        del kwargs
        rows = list(self.records.values())
        if not include_deleted:
            rows = [row for row in rows if row["state"] != "DELETED"]
        return [dict(row) for row in rows[offset : offset + limit]]


class _SnapshotOperator:
    def __getattr__(self, name):
        raise AssertionError(f"catalog deletion must not call snapshot operator: {name}")


class _SandboxLifecycle:
    def __init__(self) -> None:
        self.users: tuple[ActiveSnapshotUser, ...] = ()
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.block_start = False
        self.block_before_reservation = False
        self.query_error: ValueError | None = None

    async def list_active_snapshot_users(self, snapshot_id):
        del snapshot_id
        if self.query_error is not None:
            raise self.query_error
        return self.users

    async def get_product_view(self, sandbox_id):
        return SandboxProductView(
            sandbox_id=sandbox_id, sandbox_namespace="default", state="running",
            source_snapshot_id=None, image="", auth_token=None,
        )

    async def start_from_snapshot(
        self,
        snapshot,
        request,
        *,
        sandbox_namespace,
        sandbox_image=None,
    ):
        del snapshot, sandbox_namespace, sandbox_image
        if self.block_before_reservation:
            self.start_entered.set()
            await self.release_start.wait()
        self.users = (
            ActiveSnapshotUser(
                sandbox_id=request.sandbox_id,
                source_snapshot_id="snapshot-a",
                lifecycle_state="pending",
            ),
        )
        self.start_entered.set()
        if self.block_start:
            await self.release_start.wait()
        return {"sandbox_id": request.sandbox_id, "state": "PENDING"}


def _orchestrator(catalog: _Catalog, operator: _SnapshotOperator, lifecycle=None):
    return SnapshotManager(
        catalog,  # type: ignore[arg-type]
        operator,  # type: ignore[arg-type]
        lifecycle or _SandboxLifecycle(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_delete_only_updates_catalog_and_blocks_new_starts() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    orchestrator = _orchestrator(catalog, operator)

    deleted = await orchestrator.delete_snapshot("snapshot-a")
    assert deleted["state"] == "DELETED"
    assert deleted["deleted_at"] is not None
    assert deleted["spec"] == {"backend_payload": {"value": "snapshot-a"}}
    with pytest.raises(SnapshotNotFound):
        await orchestrator.get_snapshot_record("snapshot-a")
    with pytest.raises(SnapshotConflict, match="not REMOTE_READY"):
        await orchestrator.start_async("snapshot-a", StartFromSnapshotRequest(sandbox_id="new-runtime"))
    assert await catalog.list() == []
    assert (await catalog.list(include_deleted=True))[0]["state"] == "DELETED"
    await SnapshotReconciler(orchestrator).reconcile_once()
    assert catalog.records["snapshot-a"]["state"] == "DELETED"


@pytest.mark.asyncio
async def test_catalog_failure_preserves_snapshot_state() -> None:
    catalog = _Catalog()
    catalog.fail_delete = True
    operator = _SnapshotOperator()
    orchestrator = _orchestrator(catalog, operator)

    with pytest.raises(RuntimeError, match="catalog failure"):
        await orchestrator.delete_snapshot("snapshot-a")
    assert catalog.records["snapshot-a"]["state"] == "REMOTE_READY"
    assert catalog.records["snapshot-a"]["deleted_at"] is None


@pytest.mark.asyncio
async def test_deleted_replay_returns_the_same_catalog_record() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    orchestrator = _orchestrator(catalog, operator)

    first_result = await orchestrator.delete_snapshot("snapshot-a")
    replay_result = await orchestrator.delete_snapshot("snapshot-a")

    assert first_result == replay_result
    assert replay_result["state"] == "DELETED"


@pytest.mark.asyncio
async def test_pending_launch_reference_blocks_delete_without_waiting_for_start() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    lifecycle = _SandboxLifecycle()
    lifecycle.block_start = True
    orchestrator = _orchestrator(catalog, operator, lifecycle=lifecycle)

    start = asyncio.create_task(
        orchestrator.start_async(
            "snapshot-a",
            StartFromSnapshotRequest(sandbox_id="runtime-a"),
            sandbox_namespace="tenant-a",
        )
    )
    try:
        await asyncio.wait_for(lifecycle.start_entered.wait(), timeout=1)
        with pytest.raises(SnapshotConflict, match="runtime-a"):
            await asyncio.wait_for(orchestrator.delete_snapshot("snapshot-a"), timeout=1)
        assert not start.done()
    finally:
        lifecycle.release_start.set()
        await start
    assert catalog.records["snapshot-a"]["state"] == "REMOTE_READY"


@pytest.mark.asyncio
async def test_start_that_read_catalog_before_delete_can_finish_after_soft_delete() -> None:
    catalog = _Catalog()
    lifecycle = _SandboxLifecycle()
    lifecycle.block_before_reservation = True
    manager = _orchestrator(catalog, _SnapshotOperator(), lifecycle=lifecycle)
    start = asyncio.create_task(
        manager.start_async("snapshot-a", StartFromSnapshotRequest(sandbox_id="in-flight"))
    )
    try:
        await asyncio.wait_for(lifecycle.start_entered.wait(), timeout=1)
        deleted = await asyncio.wait_for(manager.delete_snapshot("snapshot-a"), timeout=1)
        assert deleted["state"] == "DELETED"
        assert deleted["spec"] == {"backend_payload": {"value": "snapshot-a"}}
        assert not start.done()
        with pytest.raises(SnapshotConflict, match="not REMOTE_READY"):
            await manager.start_async("snapshot-a", StartFromSnapshotRequest(sandbox_id="too-late"))
    finally:
        lifecycle.release_start.set()
        result = await start
    assert result["sandbox_id"] == "in-flight"
    assert catalog.records["snapshot-a"]["state"] == "DELETED"


@pytest.mark.asyncio
async def test_starts_from_one_snapshot_are_not_serialized() -> None:
    catalog = _Catalog()
    lifecycle = _SandboxLifecycle()
    lifecycle.block_start = True
    manager = _orchestrator(catalog, _SnapshotOperator(), lifecycle=lifecycle)
    starts = []
    try:
        for identity in ("first", "second"):
            lifecycle.start_entered.clear()
            starts.append(asyncio.create_task(
                manager.start_async("snapshot-a", StartFromSnapshotRequest(sandbox_id=identity))
            ))
            await asyncio.wait_for(lifecycle.start_entered.wait(), timeout=1)
        assert all(not start.done() for start in starts)
    finally:
        lifecycle.release_start.set()
        results = await asyncio.gather(*starts)
    assert [result["sandbox_id"] for result in results] == ["first", "second"]


@pytest.mark.asyncio
async def test_delete_reports_indeterminate_durable_usage_as_a_conflict() -> None:
    lifecycle = _SandboxLifecycle()
    lifecycle.query_error = ValueError("malformed durable runtime")
    operator = _SnapshotOperator()
    orchestrator = _orchestrator(_Catalog(), operator, lifecycle=lifecycle)

    with pytest.raises(SnapshotConflict, match="usage is indeterminate"):
        await orchestrator.delete_snapshot("snapshot-a")



@pytest.mark.asyncio
async def test_multiple_durable_sandbox_users_block_snapshot_delete() -> None:
    lifecycle = _SandboxLifecycle()
    lifecycle.users = tuple(
        ActiveSnapshotUser(
            sandbox_id=f"runtime-{index}",
            source_snapshot_id="snapshot-a",
            lifecycle_state="running",
            placement={"node_id": f"node-{index}"},
        )
        for index in (1, 2)
    )
    operator = _SnapshotOperator()
    orchestrator = _orchestrator(_Catalog(), operator, lifecycle=lifecycle)

    with pytest.raises(SnapshotConflict, match="runtime-1, runtime-2"):
        await orchestrator.delete_snapshot("snapshot-a")



@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["CREATING", "LOCAL_READY"])
async def test_delete_rejects_unfinished_snapshot(state: str) -> None:
    catalog = _Catalog()
    catalog.records["snapshot-a"]["state"] = state
    manager = _orchestrator(catalog, _SnapshotOperator())
    with pytest.raises(SnapshotConflict, match="end the source sandbox"):
        await manager.delete_snapshot("snapshot-a")
    assert catalog.records["snapshot-a"]["state"] == state


@pytest.mark.asyncio
async def test_delete_failed_snapshot() -> None:
    catalog = _Catalog()
    catalog.records["snapshot-a"]["state"] = "FAILED"
    deleted = await _orchestrator(catalog, _SnapshotOperator()).delete_snapshot("snapshot-a")
    assert deleted["state"] == "DELETED"
    assert deleted["deleted_at"] is not None


@pytest.mark.asyncio
async def test_delete_missing_snapshot() -> None:
    with pytest.raises(SnapshotNotFound):
        await _orchestrator(_Catalog(), _SnapshotOperator()).delete_snapshot("missing")
