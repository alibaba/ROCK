from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.service.snapshot_manager import (
    SnapshotConflict,
    SnapshotManager,
    SnapshotReconciler,
    SnapshotResultUnknown,
    snapshot_spec_checksum,
)
from rock.admin.snapd.client import SnapdAPIError, SnapdResultUnknown
from rock.admin.snapd.protocol import SnapdErrorCode, SnapdReply, SnapshotStatusResult
from rock.sandbox.operator.snapd import SnapdOperator


class _Catalog:
    def __init__(self, records: list[dict]) -> None:
        self.records = {item["snapshot_id"]: dict(item) for item in records}

    async def list_by_state(self, state: str, *, limit: int = 100, after=None):
        rows = sorted(self.records.values(), key=lambda row: (row["created_at"], row["snapshot_id"]))
        return [
            dict(item) for item in rows
            if item["state"] == state and (after is None or (item["created_at"], item["snapshot_id"]) > after)
        ][:limit]

    async def get(self, snapshot_id: str):
        item = self.records.get(snapshot_id)
        return None if item is None else dict(item)

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
        item = self.records[snapshot_id]
        if item["state"] == new_state or item["state"] in acceptable_replay_states:
            for key, value in (replay_identity_values or {}).items():
                if item.get(key) != value:
                    raise ValueError("snapshot transition identity conflict")
            return dict(item)
        if item["state"] != expected_state:
            return None
        item["state"] = new_state
        item.update(values or {})
        return dict(item)


class _Snapd:
    def __init__(self, statuses: dict[str, object], *, lose_first_finalize: bool = False) -> None:
        self.statuses = statuses
        self.status_calls: list[str] = []
        self.finalize_calls: list[tuple[str, str]] = []
        self.lose_first_finalize = lose_first_finalize

    async def get_snapshot_status(self, snapshot_id: str, **_kwargs):
        self.status_calls.append(snapshot_id)
        result = self.statuses.get(
            snapshot_id,
            SnapshotStatusResult(snapshot_id=snapshot_id, state="REMOTE_READY", progress=1.0),
        )
        if isinstance(result, BaseException):
            raise result
        return result.model_copy(update={"snapshot_id": snapshot_id}) if result.snapshot_id == "fixture" else result

    async def finalize_published_snapshot(self, snapshot_id: str, request, **_kwargs):
        self.finalize_calls.append((snapshot_id, request.spec_checksum))
        if self.lose_first_finalize and len(self.finalize_calls) == 1:
            raise SnapdResultUnknown("injected finalize ACK loss")
        return {"identity": snapshot_id, "state": "REMOTE_READY", "released_pins": 1}

    async def create_base_image(self, *_args, **_kwargs):
        raise AssertionError("reconciler must not start a second BASE_IMAGE build")


class _SandboxLifecycle:
    def __getattr__(self, name):
        raise AssertionError(f"snapshot reconciliation must not look up the source sandbox: {name}")


def _row(snapshot_id: str, kind: str, state: str, *, spec: dict | None = None) -> dict:
    row = {
        "snapshot_id": snapshot_id,
        "source_worker_ip": "10.0.0.1",
        "kind": kind,
        "state": state,
        "spec": spec,
        "format_version": 1,
        "created_at": datetime.datetime(2026, 8, 22, tzinfo=datetime.timezone.utc),
    }
    if kind == "FULL":
        row.update(source_sandbox_id="source")
    return row


def _spec(snapshot_id: str, kind: str) -> dict:
    return {"backend_payload": {"value": snapshot_id, "variant": kind, "nested": [None, "opaque"]}}


def _status(state: str, *, spec: dict | None = None):
    return SnapshotStatusResult(
        snapshot_id="fixture",
        state=state,
        progress=1.0 if state == "REMOTE_READY" else 0.5,
        snapshot_spec=spec,
        rootfs_bytes=67108864 if state == "REMOTE_READY" else None,
    )


def _manager(catalog: _Catalog, snapd: _Snapd, *, lifecycle=None, pool=None) -> SnapshotManager:
    class _Pool:
        def client(self, host_ip):
            if not host_ip:
                raise SnapdResultUnknown("snapshot source worker IP is unavailable")
            assert host_ip == "10.0.0.1"
            return snapd

    return SnapshotManager(
        catalog,  # type: ignore[arg-type]
        SnapdOperator(
            MagicMock(),
            _client_pool=pool or _Pool(),  # type: ignore[arg-type]
        ),  # type: ignore[arg-type]
        lifecycle or _SandboxLifecycle(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_reconciler_commits_opaque_spec_and_independent_size_summaries() -> None:
    spec = {"backend_v2": {"objects": [None, "快照"]}}
    row = _row("opaque", "FULL", "LOCAL_READY")
    row["resource_spec"] = {"vcpus": 2, "memory_mib": 128}
    catalog = _Catalog([row])
    snapd = _Snapd({"opaque": SnapshotStatusResult(
        snapshot_id="opaque", state="REMOTE_READY", progress=1, snapshot_spec=spec,
        memory_bytes=134217728, rootfs_bytes=67108864,
    )})
    assert await SnapshotReconciler(_manager(catalog, snapd)).reconcile_once() == 1
    assert catalog.records["opaque"]["spec"] == spec
    assert catalog.records["opaque"]["resource_spec"] == {
        "vcpus": 2, "memory_mib": 128, "memory_bytes": 134217728, "rootfs_bytes": 67108864,
    }
    assert snapd.finalize_calls == [("opaque", snapshot_spec_checksum(spec))]


@pytest.mark.asyncio
async def test_reconciler_rejects_another_snapshot_status_identity() -> None:
    row = _row("expected", "FULL", "LOCAL_READY")
    catalog = _Catalog([row])
    snapd = _Snapd({"expected": SnapshotStatusResult(
        snapshot_id="another", state="REMOTE_READY", progress=1, snapshot_spec={},
    )})
    reconciler = SnapshotReconciler(_manager(catalog, snapd))
    with pytest.raises(SnapshotConflict, match="another snapshot identity"):
        await reconciler._snapshot_status(row)
    assert catalog.records["expected"] == row
    assert snapd.finalize_calls == []


@pytest.mark.asyncio
async def test_reconciler_polls_creating_base_and_cas_converges_all_stable_states() -> None:
    ready_spec = _spec("base-ready", "BASE_IMAGE")
    existing_spec = {"format_version": 1, "identity": "existing"}
    catalog = _Catalog(
        [
            _row("base-local", "BASE_IMAGE", "CREATING"),
            _row("base-ready", "BASE_IMAGE", "CREATING"),
            _row("full-creating", "FULL", "CREATING"),
            _row("lost-local", "FULL", "LOCAL_READY"),
            _row("retry-local", "FULL", "LOCAL_READY"),
            _row("already-ready", "FULL", "REMOTE_READY", spec=existing_spec),
        ]
    )
    snapd = _Snapd(
        {
            "base-local": _status("LOCAL_READY"),
            "base-ready": _status("REMOTE_READY", spec=ready_spec),
            "full-creating": _status("LOCAL_READY"),
            "lost-local": _status("FAILED"),
            "retry-local": _status("LOCAL_READY"),
        }
    )
    orchestrator = _manager(catalog, snapd)

    assert await SnapshotReconciler(orchestrator).reconcile_once() == 4
    assert catalog.records["base-local"]["state"] == "LOCAL_READY"
    assert catalog.records["base-ready"]["state"] == "REMOTE_READY"
    assert catalog.records["base-ready"]["spec"] == ready_spec
    assert catalog.records["base-ready"]["resource_spec"] == {"rootfs_bytes": 67108864}
    assert catalog.records["base-ready"]["remote_ready_at"].tzinfo is not None
    assert catalog.records["lost-local"]["state"] == "FAILED"
    assert catalog.records["retry-local"]["state"] == "LOCAL_READY"
    assert catalog.records["full-creating"]["state"] == "LOCAL_READY"
    assert [snapshot_id for snapshot_id, _checksum in snapd.finalize_calls] == [
        "base-ready",
        "already-ready",
    ]


@pytest.mark.asyncio
async def test_reconciler_isolates_unknown_creating_job_without_blocking_later_rows() -> None:
    ready_spec = _spec("new-base", "BASE_IMAGE")
    catalog = _Catalog(
        [
            _row("lost-fork", "FULL", "CREATING"),
            _row("new-base", "BASE_IMAGE", "CREATING"),
        ]
    )
    snapd = _Snapd(
        {
            "lost-fork": SnapdAPIError(
                SnapdReply(
                    request_id="lost-status",
                    code=SnapdErrorCode.NOT_FOUND,
                    error_message="snapshot control object was not found",
                )
            ),
            "new-base": _status("REMOTE_READY", spec=ready_spec),
        }
    )

    assert await SnapshotReconciler(_manager(catalog, snapd)).reconcile_once() == 1
    assert catalog.records["lost-fork"]["state"] == "CREATING"
    assert catalog.records["new-base"]["state"] == "REMOTE_READY"
    assert [snapshot_id for snapshot_id in snapd.status_calls] == [
        "lost-fork",
        "new-base",
    ]


@pytest.mark.asyncio
async def test_reconciler_isolates_unreachable_durable_owner_without_trying_another_worker() -> None:
    ready_spec = _spec("new-base", "BASE_IMAGE")
    catalog = _Catalog(
        [
            _row("stale-creating", "FULL", "CREATING"),
            _row("new-base", "BASE_IMAGE", "LOCAL_READY"),
            _row("stale-ready", "FULL", "REMOTE_READY", spec=_spec("stale-ready", "FULL")),
        ]
    )
    snapd = _Snapd({"new-base": _status("REMOTE_READY", spec=ready_spec)})

    catalog.records["new-base"]["source_worker_ip"] = "10.0.0.2"
    contacted = []

    class _Pool:
        def client(self, host_ip):
            contacted.append(host_ip)
            if host_ip == "10.0.0.1":
                raise SnapdResultUnknown("durable owner is unreachable")
            assert host_ip == "10.0.0.2"
            return snapd

    manager = _manager(catalog, snapd, pool=_Pool())

    assert await SnapshotReconciler(manager).reconcile_once() == 1
    assert catalog.records["stale-creating"]["state"] == "CREATING"
    assert catalog.records["stale-ready"]["state"] == "REMOTE_READY"
    assert catalog.records["new-base"]["state"] == "REMOTE_READY"
    assert [snapshot_id for snapshot_id, _checksum in snapd.finalize_calls] == ["new-base"]
    assert contacted == ["10.0.0.1", "10.0.0.2", "10.0.0.2", "10.0.0.1"]


@pytest.mark.parametrize("source_state", ["deleted", "moved"])
async def test_snapshot_observation_and_finalize_keep_catalog_owner_after_source_changes(source_state) -> None:
    snapshot_id = "old-owner-snapshot"
    spec = _spec(snapshot_id, "FULL")
    catalog = _Catalog([_row(snapshot_id, "FULL", "LOCAL_READY")])
    snapd = _Snapd({snapshot_id: _status("REMOTE_READY", spec=spec)})
    lifecycle = MagicMock()
    lifecycle.require_snapshot_source = AsyncMock(
        side_effect=ValueError("source sandbox was deleted") if source_state == "deleted" else None,
        return_value=MagicMock(target=MagicMock(host_ip="10.0.0.99")),
    )
    contacted = []

    class _Pool:
        def client(self, host_ip):
            contacted.append(host_ip)
            assert host_ip == "10.0.0.1"
            return snapd

    reconciler = SnapshotReconciler(_manager(catalog, snapd, lifecycle=lifecycle, pool=_Pool()))
    assert await reconciler.reconcile_once() == 1

    assert catalog.records[snapshot_id]["state"] == "REMOTE_READY"
    assert contacted == ["10.0.0.1", "10.0.0.1"]
    lifecycle.require_snapshot_source.assert_not_awaited()
    assert len(snapd.status_calls) == len(snapd.finalize_calls) == 1


@pytest.mark.parametrize("owner", [None, ""])
@pytest.mark.parametrize("operation", ["status", "finalize"])
async def test_snapshot_with_missing_owner_stays_unknown_without_source_lookup(owner, operation) -> None:
    spec = _spec("missing-owner", "FULL")
    row = _row("missing-owner", "FULL", "REMOTE_READY", spec=spec)
    row["source_worker_ip"] = owner
    snapd = _Snapd({})
    reconciler = SnapshotReconciler(_manager(_Catalog([row]), snapd))

    with pytest.raises(SnapshotResultUnknown, match="worker IP"):
        if operation == "status":
            await reconciler._snapshot_status(row)
        else:
            await reconciler._finalize_published(row, spec)

    assert snapd.status_calls == snapd.finalize_calls == []
    assert reconciler._finalized_checksums == {}


@pytest.mark.asyncio
async def test_finalize_ack_loss_replays_same_identity_without_republishing() -> None:
    spec = _spec("snapshot-ack-loss", "FULL")
    catalog = _Catalog([_row("snapshot-ack-loss", "FULL", "LOCAL_READY")])
    snapd = _Snapd(
        {"snapshot-ack-loss": _status("REMOTE_READY", spec=spec)},
        lose_first_finalize=True,
    )
    reconciler = SnapshotReconciler(_manager(catalog, snapd))

    assert await reconciler.reconcile_once() == 0
    assert catalog.records["snapshot-ack-loss"]["state"] == "REMOTE_READY"
    assert await reconciler.reconcile_once() == 0
    assert len(snapd.finalize_calls) == 2
    assert snapd.finalize_calls[0] == snapd.finalize_calls[1]


@pytest.mark.asyncio
async def test_upload_ready_does_not_finalize_before_catalog_commit() -> None:
    spec = _spec("snapshot-before-commit", "FULL")

    class _CommitFailureCatalog(_Catalog):
        async def conditional_transition(self, snapshot_id, **kwargs):
            if kwargs.get("new_state") == "REMOTE_READY":
                raise RuntimeError("injected catalog commit failure")
            return await super().conditional_transition(snapshot_id, **kwargs)

    catalog = _CommitFailureCatalog([_row("snapshot-before-commit", "FULL", "LOCAL_READY")])
    snapd = _Snapd({"snapshot-before-commit": _status("REMOTE_READY", spec=spec)})

    with pytest.raises(RuntimeError, match="catalog commit failure"):
        await SnapshotReconciler(_manager(catalog, snapd)).reconcile_once()

    assert catalog.records["snapshot-before-commit"]["state"] == "LOCAL_READY"
    assert snapd.finalize_calls == []


@pytest.mark.asyncio
async def test_persisted_remote_ready_row_finalizes_after_admin_restart() -> None:
    spec = _spec("snapshot-after-commit", "FULL")
    catalog = _Catalog([_row("snapshot-after-commit", "FULL", "REMOTE_READY", spec=spec)])
    snapd = _Snapd({})

    assert await SnapshotReconciler(_manager(catalog, snapd)).reconcile_once() == 0

    assert len(snapd.finalize_calls) == 1
    assert snapd.status_calls == []
    snapshot_id, checksum = snapd.finalize_calls[0]
    assert snapshot_id == "snapshot-after-commit"
    assert checksum.startswith("sha256:")


@pytest.mark.parametrize("state", ["CREATING", "LOCAL_READY"])
async def test_reconciler_reaches_ready_row_after_100_unfinished_rows(state) -> None:
    identities = [f"snapshot-{index:03d}" for index in range(101)]
    catalog = _Catalog([_row(snapshot_id, "BASE_IMAGE", state) for snapshot_id in reversed(identities)])
    statuses = {snapshot_id: _status(state) for snapshot_id in identities}
    statuses[identities[-1]] = _status("REMOTE_READY", spec=_spec(identities[-1], "BASE_IMAGE"))
    snapd = _Snapd(statuses)
    reconciler = SnapshotReconciler(_manager(catalog, snapd))

    await reconciler.reconcile_once()
    assert catalog.records[identities[-1]]["state"] == state
    assert identities[-1] not in {snapshot_id for snapshot_id in snapd.status_calls}
    await reconciler.reconcile_once()

    assert catalog.records[identities[-1]]["state"] == "REMOTE_READY"
    assert any(snapshot_id == identities[-1] for snapshot_id, _ in snapd.finalize_calls)


async def test_remote_ready_scan_advances_past_cached_rows_and_wraps_to_retry_failures() -> None:
    identities = [f"snapshot-{index:03d}" for index in range(101)]
    catalog = _Catalog([
        _row(snapshot_id, "BASE_IMAGE", "REMOTE_READY", spec=_spec(snapshot_id, "BASE_IMAGE"))
        for snapshot_id in reversed(identities)
    ])
    snapd = _Snapd({}, lose_first_finalize=True)
    manager = _manager(catalog, snapd)
    reconciler = SnapshotReconciler(manager)

    await reconciler.reconcile_once()
    assert len(snapd.finalize_calls) == 100
    await reconciler.reconcile_once()
    assert snapd.finalize_calls[-1][0] == identities[-1]
    await reconciler.reconcile_once()
    assert snapd.finalize_calls[-1][0] == identities[0]
    assert len(snapd.finalize_calls) == 102
    await reconciler.reconcile_once()
    assert len(snapd.finalize_calls) == 102

    # A fresh Admin starts a new bounded sweep, including rows after page one.
    restarted = SnapshotReconciler(manager)
    await restarted.reconcile_once()
    await restarted.reconcile_once()
    assert {snapshot_id for snapshot_id, _ in snapd.finalize_calls[102:]} == set(identities)


async def test_state_cursors_are_independent_and_wrap_for_earlier_rows_and_empty_states() -> None:
    states = ("CREATING", "LOCAL_READY", "REMOTE_READY")
    catalog = _Catalog([
        _row(f"{state}-{index}", "BASE_IMAGE", state)
        for state in states for index in range(2)
    ])
    reconciler = SnapshotReconciler(_manager(catalog, _Snapd({})))
    for state in states:
        first = await reconciler._next_batch(state, 1)
        assert first[0]["snapshot_id"] == f"{state}-0"
        reconciler._scan_cursors[state] = (first[0]["created_at"], first[0]["snapshot_id"])
    for state in states:
        # Moving/deleting the earlier page must not shift later rows away.
        del catalog.records[f"{state}-0"]
        second = await reconciler._next_batch(state, 1)
        assert second[0]["snapshot_id"] == f"{state}-1"
        reconciler._scan_cursors[state] = (second[0]["created_at"], second[0]["snapshot_id"])
        earlier = _row(f"{state}-new", "BASE_IMAGE", state)
        earlier["created_at"] -= datetime.timedelta(seconds=1)
        catalog.records[earlier["snapshot_id"]] = earlier
        wrapped = await reconciler._next_batch(state, 1)
        assert wrapped[0]["snapshot_id"] == earlier["snapshot_id"]
        reconciler._scan_cursors[state] = (wrapped[0]["created_at"], wrapped[0]["snapshot_id"])
    catalog.records.clear()
    for state in states:
        assert await reconciler._next_batch(state, 1) == []
    assert reconciler._scan_cursors == {}


@pytest.mark.parametrize("state", ["CREATING", "REMOTE_READY"])
async def test_unexpected_row_error_does_not_skip_unattempted_rows_in_the_page(state, monkeypatch) -> None:
    catalog = _Catalog([
        _row(snapshot_id, "BASE_IMAGE", state, spec=_spec(snapshot_id, "BASE_IMAGE"))
        for snapshot_id in ("a", "b", "c")
    ])
    snapd = _Snapd({snapshot_id: _status("CREATING") for snapshot_id in ("a", "b", "c")})
    reconciler = SnapshotReconciler(_manager(catalog, snapd))
    attempted = []

    async def fail_first_row(row, _status_or_spec):
        attempted.append(row["snapshot_id"])
        if row["snapshot_id"] == "a":
            raise RuntimeError("catalog operation failed")
        return False

    method = "_converge_status" if state == "CREATING" else "_finalize_published"
    monkeypatch.setattr(reconciler, method, fail_first_row)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="catalog operation failed"):
            await reconciler.reconcile_once(limit=2)
        await reconciler.reconcile_once(limit=2)

    assert attempted == ["a", "b", "c", "a", "b", "c"]
