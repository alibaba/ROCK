import asyncio
import datetime
from dataclasses import replace

import pytest
from sqlalchemy.exc import DisconnectionError, IntegrityError, InterfaceError, OperationalError

from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.core.schema import RockSnapshotRecord, SandboxRecord
from rock.admin.core.snapshot_table import BaseImageResolutionIdentity, SnapshotIdentityConflict, SnapshotTable

UTC_NOW = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)

BASE_IDENTITY = BaseImageResolutionIdentity(
    oci_ref="registry/image:original",
    oci_digest="sha256:" + "a" * 64,
    oci_platform="linux/amd64",
    base_image_builder_version="builder-v1",
    base_image_build_key="sha256:" + "b" * 64,
    source_worker_ip="10.0.0.1",
)


@pytest.fixture(params=[
    OperationalError, InterfaceError, DisconnectionError, ConnectionError, OSError, asyncio.TimeoutError,
])
def disconnect_error(request):
    if request.param in (OperationalError, InterfaceError):
        return request.param("commit", {}, ConnectionError("ack lost"))
    return request.param("ack lost")


async def test_base_image_claim_preserves_canonical_request_without_persistent_alias(db_provider):
    table = SnapshotTable(db_provider)
    first = await table.claim_base_image(snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW)
    alias = await table.claim_base_image(
        snapshot_id="alias", created_at=UTC_NOW,
        resolution=replace(BASE_IDENTITY, oci_ref="registry/image:alias", source_worker_ip="10.0.0.2"),
    )

    assert not first.reused and first.dispatch
    assert alias.reused and alias.dispatch
    assert alias.snapshot == first.snapshot
    assert await table.get("alias") is None
    # A formerly supplied alias is not reserved after joining another artifact.
    changed = await table.claim_base_image(
        snapshot_id="alias", created_at=UTC_NOW,
        resolution=replace(
            BASE_IDENTITY, oci_digest="sha256:" + "c" * 64, base_image_build_key="sha256:" + "d" * 64,
        ),
    )
    assert changed.snapshot["snapshot_id"] == "alias"
    assert not changed.reused


@pytest.mark.parametrize("field,value", [
    ("oci_digest", "sha256:" + "c" * 64),
    ("oci_platform", "linux/arm64/v8"),
    ("base_image_builder_version", "builder-v2"),
    ("base_image_build_key", "sha256:" + "d" * 64),
])
async def test_base_image_claim_rejects_actual_id_with_changed_content(db_provider, field, value):
    table = SnapshotTable(db_provider)
    await table.claim_base_image(snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW)
    with pytest.raises(SnapshotIdentityConflict, match="another immutable OCI identity"):
        await table.claim_base_image(
            snapshot_id="first", resolution=replace(BASE_IDENTITY, **{field: value}), created_at=UTC_NOW,
        )


async def test_base_image_claim_rejects_actual_id_with_changed_format(db_provider):
    table = SnapshotTable(db_provider)
    await table.claim_base_image(snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW)
    with pytest.raises(SnapshotIdentityConflict, match="another immutable OCI identity"):
        await table.claim_base_image(
            snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW, format_version=2,
        )


@pytest.mark.parametrize("kind,state", [("FULL", "CREATING"), ("BASE_IMAGE", "DELETED")])
async def test_base_image_claim_rejects_other_kind_or_deleted_actual_id(db_provider, kind, state):
    with db_provider.session_factory() as session, session.begin():
        session.add(RockSnapshotRecord(
            snapshot_id="first", kind=kind, state=state, created_at=UTC_NOW,
            source_sandbox_id="sandbox" if kind == "FULL" else None,
            deleted_at=UTC_NOW if state == "DELETED" else None,
        ))
    with pytest.raises(SnapshotIdentityConflict, match="already bound"):
        await SnapshotTable(db_provider).claim_base_image(
            snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW,
        )


async def test_base_image_claim_retries_failed_canonical_with_unchanged_input(db_provider):
    table = SnapshotTable(db_provider)
    first = await table.claim_base_image(snapshot_id="first", resolution=BASE_IDENTITY, created_at=UTC_NOW)
    with db_provider.session_factory() as session, session.begin():
        session.get(RockSnapshotRecord, "first").state = "FAILED"
    retry = await table.claim_base_image(
        snapshot_id="retry", created_at=UTC_NOW,
        resolution=replace(BASE_IDENTITY, oci_ref="registry/image:retry", source_worker_ip="10.0.0.2"),
    )
    assert retry.dispatch and retry.reused
    assert retry.snapshot == first.snapshot


def _insert_running_source(db_provider, sandbox_id: str, *, namespace: str = "tenant-a", epoch: int = 7) -> None:
    with db_provider.session_factory() as session:
        session.add(
            SandboxRecord(
                sandbox_id=sandbox_id,
                namespace=namespace,
                state="running",
                status={
                    "host_ip": "10.0.0.1",
                    "snapd_node_id": "node-a",
                },
            )
        )
        session.commit()


async def test_create_and_get_base_image(db_provider):
    table = SnapshotTable(db_provider)

    created = await table.create(
        snapshot_id="base-1",
        kind="BASE_IMAGE",
        created_at=UTC_NOW,
    )

    assert created == {
        "snapshot_id": "base-1",
        "kind": "BASE_IMAGE",
        "source_sandbox_id": None,
        "source_worker_ip": None,
        "format_version": 1,
        "oci_ref": None,
        "oci_digest": None,
        "oci_platform": None,
        "base_image_builder_version": None,
        "base_image_build_key": None,
        "product_metadata": None,
        "resource_spec": None,
        "spec": None,
        "state": "CREATING",
        "created_at": UTC_NOW,
        "remote_ready_at": None,
        "deleted_at": None,
    }
    assert await table.get("base-1") == created


async def test_snapshot_lookup_and_listing_use_cluster_global_ids(db_provider):
    table = SnapshotTable(db_provider)
    await table.create(
        snapshot_id="tenant-a-snapshot",
        kind="BASE_IMAGE",
        created_at=UTC_NOW,
    )
    await table.create(
        snapshot_id="tenant-b-snapshot",
        kind="BASE_IMAGE",
        created_at=UTC_NOW,
    )

    assert (await table.get("tenant-a-snapshot"))["snapshot_id"] == "tenant-a-snapshot"
    records = await table.list()
    assert {record["snapshot_id"] for record in records} == {"tenant-a-snapshot", "tenant-b-snapshot"}


async def test_source_worker_ip_is_normalized_at_creation(db_provider):
    table = SnapshotTable(db_provider)
    with pytest.raises(ValueError, match="IP"):
        await table.create(
            snapshot_id="invalid-worker",
            kind="BASE_IMAGE",
            source_worker_ip="snapd.internal",
            created_at=UTC_NOW,
        )

    first = await table.create(
        snapshot_id="base-worker", kind="BASE_IMAGE", source_worker_ip="2001:db8:0:0::7", created_at=UTC_NOW,
    )
    assert first["source_worker_ip"] == "2001:db8::7"


async def _attempt(table, snapshot_id):
    return await table.create_full_snapshot_attempt(
        snapshot_id=snapshot_id,
        source_sandbox_id="source",
        source_worker_ip="10.0.0.1",
        resource_spec={"vcpus": 2, "memory_mib": 256},
        created_at=UTC_NOW,
    )


async def test_concurrent_creating_attempts_do_not_lock_or_write_source(db_provider):
    table = SnapshotTable(db_provider)
    _insert_running_source(db_provider, "source")
    statements = []
    from sqlalchemy import event

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db_provider.engine, "before_cursor_execute", record_statement)
    try:
        first = await _attempt(table, "full-a")
        second = await _attempt(table, "full-b")
    finally:
        event.remove(db_provider.engine, "before_cursor_execute", record_statement)
    assert first["state"] == second["state"] == "CREATING"
    assert first["snapshot_id"] != second["snapshot_id"]
    assert "generation" not in first and "generation" not in second
    assert not any("sandbox_record" in statement for statement in statements)


async def test_full_snapshot_attempt_copies_frozen_resources_once(db_provider):
    table = SnapshotTable(db_provider)
    resources = {"vcpus": 3, "memory_mib": 768, "disk": "4G"}
    record = await table.create_full_snapshot_attempt(
        snapshot_id="frozen-resources",
        source_sandbox_id="source",
        source_worker_ip="10.0.0.1",
        resource_spec=resources,
        created_at=UTC_NOW,
    )
    resources["vcpus"] = 9
    assert record["resource_spec"] == {"vcpus": 3, "memory_mib": 768, "disk": "4G"}
    assert (await table.get("frozen-resources"))["resource_spec"] == record["resource_spec"]
    with pytest.raises(ValueError, match="unsupported snapshot transition values"):
        await table.conditional_transition(
            "frozen-resources", expected_state="CREATING", new_state="LOCAL_READY", values={"resource_spec": resources},
        )


async def test_distinct_cuts_from_same_source_can_all_become_ready(db_provider):
    table = SnapshotTable(db_provider)
    await _attempt(table, "winner")
    await _attempt(table, "loser")
    await table.conditional_transition("winner", expected_state="CREATING", new_state="LOCAL_READY")
    await table.conditional_transition("loser", expected_state="CREATING", new_state="LOCAL_READY")
    restarted = await _attempt(table, "restarted-source")
    assert restarted["state"] == "CREATING"
    await table.conditional_transition("restarted-source", expected_state="CREATING", new_state="LOCAL_READY")


async def test_attempt_id_collision_does_not_replay_or_change_original_record(db_provider):
    table = SnapshotTable(db_provider)
    first = await _attempt(table, "replay")
    with pytest.raises(SnapshotIdentityConflict, match="already exists"):
        await _attempt(table, "replay")
    assert await table.get("replay") == first
    assert "runtime_epoch" not in first
    assert "operation_identity" not in first


async def test_generic_full_create_also_rejects_duplicate_id(db_provider):
    table = SnapshotTable(db_provider)
    first = await table.create(
        snapshot_id="full-direct", kind="FULL", source_sandbox_id="source", created_at=UTC_NOW,
    )
    with pytest.raises(SnapshotIdentityConflict, match="already exists"):
        await table.create(
            snapshot_id="full-direct", kind="FULL", source_sandbox_id="source", created_at=UTC_NOW,
        )
    assert await table.get("full-direct") == first


@pytest.mark.parametrize("state", ["CREATING", "LOCAL_READY", "FAILED"])
async def test_duplicate_id_never_mutates_existing_attempt(db_provider, state):
    table = SnapshotTable(db_provider)
    first = await _attempt(table, "winner")
    if state != "CREATING":
        await table.conditional_transition("winner", expected_state="CREATING", new_state=state)
    with pytest.raises(SnapshotIdentityConflict, match="already exists"):
        await _attempt(table, "winner")
    assert (await table.get("winner"))["state"] == state
    assert (await table.get("winner"))["created_at"] == first["created_at"]


async def test_failed_attempt_does_not_prevent_another_cut(db_provider):
    table = SnapshotTable(db_provider)
    await _attempt(table, "failed")
    await table.conditional_transition("failed", expected_state="CREATING", new_state="FAILED")
    retry = await _attempt(table, "retry")
    assert retry["state"] == "CREATING"


async def test_attempt_commit_ack_loss_is_propagated_without_replay(db_provider, monkeypatch):
    table = SnapshotTable(db_provider)
    original = table._create_sync
    calls = 0

    def lose_ack(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            raise OperationalError("lost commit acknowledgement", {}, Exception("connection lost"))
        return result

    monkeypatch.setattr(table, "_create_sync", lose_ack)
    with pytest.raises(OperationalError, match="lost commit acknowledgement"):
        await _attempt(table, "ack-lost")
    assert calls == 1
    row = await table.get("ack-lost")
    assert row["state"] == "CREATING"


async def _no_sleep(_delay):
    pass


@pytest.mark.parametrize(
    ("kind", "source_sandbox_id"),
    [
        ("BASE_IMAGE", "sandbox-1"),
        ("FULL", None),
    ],
)
async def test_kind_source_geometry_is_enforced(db_provider, kind, source_sandbox_id):
    table = SnapshotTable(db_provider)

    with pytest.raises(IntegrityError):
        await table.create(
            snapshot_id=f"invalid-{kind}-{source_sandbox_id}",
            kind=kind,
            source_sandbox_id=source_sandbox_id,
            created_at=UTC_NOW,
        )


async def test_conditional_transition_is_compare_and_swap(db_provider):
    table = SnapshotTable(db_provider)
    await table.create(
        snapshot_id="full-cas",
        kind="FULL",
        source_sandbox_id="sandbox-cas",
        created_at=UTC_NOW,
    )

    transitioned = await table.conditional_transition(
        "full-cas",
        expected_state="CREATING",
        new_state="LOCAL_READY",
    )
    stale_writer = await table.conditional_transition(
        "full-cas",
        expected_state="CREATING",
        new_state="FAILED",
    )

    assert transitioned is not None
    assert transitioned["state"] == "LOCAL_READY"
    assert stale_writer is None
    assert (await table.get("full-cas"))["state"] == "LOCAL_READY"


async def test_remote_ready_transition_sets_spec_and_timestamp_atomically(db_provider):
    table = SnapshotTable(db_provider)
    await table.create(
        snapshot_id="full-remote",
        kind="FULL",
        source_sandbox_id="sandbox-remote",
        created_at=UTC_NOW,
    )
    await table.conditional_transition(
        "full-remote",
        expected_state="CREATING",
        new_state="LOCAL_READY",
    )
    spec = {"backend_payload": {"value": "full-remote", "nested": [None, 1]}}
    ready_at = UTC_NOW + datetime.timedelta(minutes=2)

    result = await table.conditional_transition(
        "full-remote",
        expected_state="LOCAL_READY",
        new_state="REMOTE_READY",
        values={"spec": spec, "remote_ready_at": ready_at},
    )

    assert result is not None
    assert result["state"] == "REMOTE_READY"
    assert result["spec"] == spec
    assert result["remote_ready_at"] == ready_at


async def test_list_by_state_is_oldest_first_and_limited(db_provider):
    table = SnapshotTable(db_provider)
    for index in range(3):
        await table.create(
            snapshot_id=f"snapshot-{index}",
            kind="BASE_IMAGE",
            created_at=UTC_NOW + datetime.timedelta(seconds=index),
        )
        await table.conditional_transition(
            f"snapshot-{index}",
            expected_state="CREATING",
            new_state="LOCAL_READY",
        )

    rows = await table.list_by_state("LOCAL_READY", limit=2)

    assert [row["snapshot_id"] for row in rows] == ["snapshot-0", "snapshot-1"]


async def test_state_cursor_orders_tied_timestamps_and_survives_earlier_state_changes(db_provider):
    table = SnapshotTable(db_provider)
    for snapshot_id in ("snapshot-c", "snapshot-a", "snapshot-b"):
        await table.create(snapshot_id=snapshot_id, kind="BASE_IMAGE", created_at=UTC_NOW)
    await table.create(
        snapshot_id="newer", kind="BASE_IMAGE", created_at=UTC_NOW + datetime.timedelta(seconds=1),
    )
    first = await table.list_by_state("CREATING", limit=2)
    assert [row["snapshot_id"] for row in first] == ["snapshot-a", "snapshot-b"]
    cursor = (first[-1]["created_at"], first[-1]["snapshot_id"])
    for row in first:
        await table.conditional_transition(row["snapshot_id"], expected_state="CREATING", new_state="FAILED")

    second = await table.list_by_state("CREATING", limit=2, after=cursor)

    assert [row["snapshot_id"] for row in second] == ["snapshot-c", "newer"]
    assert await table.list_by_state(
        "CREATING", limit=2, after=(second[-1]["created_at"], second[-1]["snapshot_id"]),
    ) == []


async def test_state_cursor_does_not_require_the_cursor_row_to_still_exist(db_provider):
    table = SnapshotTable(db_provider)
    for snapshot_id in ("snapshot-a", "snapshot-b"):
        await table.create(snapshot_id=snapshot_id, kind="BASE_IMAGE", created_at=UTC_NOW)
    first = (await table.list_by_state("CREATING", limit=1))[0]
    with db_provider.session_factory() as session, session.begin():
        session.delete(session.get(RockSnapshotRecord, first["snapshot_id"]))

    rows = await table.list_by_state("CREATING", limit=1, after=(first["created_at"], first["snapshot_id"]))

    assert [row["snapshot_id"] for row in rows] == ["snapshot-b"]


async def test_sandbox_source_snapshot_id_is_persisted_as_a_scalar_column(db_provider):
    snapshots = SnapshotTable(db_provider)
    sandboxes = SandboxTable(db_provider)
    await snapshots.create(snapshot_id="base-source", kind="BASE_IMAGE", created_at=UTC_NOW)

    await sandboxes.create("sandbox-from-snapshot", {"source_snapshot_id": "base-source"})

    record = await sandboxes.get("sandbox-from-snapshot")
    assert record is not None
    assert record["source_snapshot_id"] == "base-source"
    assert (await sandboxes.list_by("source_snapshot_id", "base-source"))[0]["sandbox_id"] == "sandbox-from-snapshot"


async def test_snapshot_execution_metadata_is_durable_without_shadow_desired_state(db_provider):
    snapshots = SnapshotTable(db_provider)
    sandboxes = SandboxTable(db_provider)
    await snapshots.create(snapshot_id="base-placement", kind="BASE_IMAGE", created_at=UTC_NOW)
    await sandboxes.create(
        "sandbox-placement",
        {
            "source_snapshot_id": "base-placement",
            "snapd_node_id": "snapd-a",
            "host_ip": "10.0.0.1",
        },
    )

    row = await sandboxes.get("sandbox-placement")
    assert row is not None
    assert row["snapd_node_id"] == "snapd-a"
    assert row["host_ip"] == "10.0.0.1"
    assert "snapshot_runtime" not in row


async def test_active_snapshot_users_come_from_durable_runtime_rows(db_provider):
    snapshots = SnapshotTable(db_provider)
    sandboxes = SandboxTable(db_provider)
    await snapshots.create(snapshot_id="base-users", kind="BASE_IMAGE", created_at=UTC_NOW)

    for sandbox_id, state, epoch in (
        ("pending-user", "pending", 3),
        ("running-user", "running", 4),
        ("cleanup-failed-user", "cleanup_failed", 5),
        ("stopped-user", "stopped", 6),
        ("deleted-user", "deleted", 7),
    ):
        await sandboxes.create(
            sandbox_id,
            {
                "source_snapshot_id": "base-users",
                "namespace": "tenant-a",
                "state": state,
                "snapd_node_id": f"node-{epoch}" if state == "running" else None,
            },
        )
    await sandboxes.create(
        "other-tenant",
        {
            "source_snapshot_id": "base-users",
            "namespace": "tenant-b",
            "state": "running",
        },
    )

    users = await sandboxes.list_snapshot_references("base-users")

    assert [(user["sandbox_id"], user["state"]) for user in users] == [
        ("cleanup-failed-user", "cleanup_failed"),
        ("deleted-user", "deleted"),
        ("other-tenant", "running"),
        ("pending-user", "pending"),
        ("running-user", "running"),
        ("stopped-user", "stopped"),
    ]
    assert users[4]["snapd_node_id"] == "node-4"


async def test_pending_snapshot_user_needs_no_runtime_envelope(db_provider):
    snapshots = SnapshotTable(db_provider)
    sandboxes = SandboxTable(db_provider)
    await snapshots.create(snapshot_id="base-malformed", kind="BASE_IMAGE", created_at=UTC_NOW)
    await sandboxes.create(
        "malformed-user",
        {
            "source_snapshot_id": "base-malformed",
            "namespace": "tenant-a",
            "state": "pending",
        },
    )

    users = await sandboxes.list_snapshot_references("base-malformed")
    assert [user["sandbox_id"] for user in users] == ["malformed-user"]


async def test_create_reads_back_commit_after_connection_ack_is_lost(db_provider, disconnect_error):
    table = SnapshotTable(db_provider)
    original_run = db_provider.run
    injected = False

    async def lose_ack_after_run(fn, *args, **kwargs):
        nonlocal injected
        result = await original_run(fn, *args, **kwargs)
        if fn.__name__ == "_create_sync" and not injected:
            injected = True
            raise disconnect_error
        return result

    db_provider.run = lose_ack_after_run
    result = await table.create(snapshot_id="base-ack-lost", kind="BASE_IMAGE", created_at=UTC_NOW)

    assert injected
    assert result["snapshot_id"] == "base-ack-lost"
    assert result["state"] == "CREATING"


async def test_create_replays_existing_identity_after_state_has_advanced(db_provider):
    table = SnapshotTable(db_provider)
    await table.create(snapshot_id="base-replay", kind="BASE_IMAGE", created_at=UTC_NOW)
    await table.conditional_transition("base-replay", expected_state="CREATING", new_state="LOCAL_READY")

    replay = await table.create(
        snapshot_id="base-replay",
        kind="BASE_IMAGE",
        created_at=UTC_NOW + datetime.timedelta(hours=1),
    )

    assert replay["state"] == "LOCAL_READY"
    assert replay["created_at"] == UTC_NOW


async def test_transition_reads_back_commit_after_connection_ack_is_lost(db_provider, disconnect_error):
    table = SnapshotTable(db_provider)
    await table.create(snapshot_id="transition-ack-lost", kind="BASE_IMAGE", created_at=UTC_NOW)
    original_run = db_provider.run
    injected = False

    async def lose_ack_after_run(fn, *args, **kwargs):
        nonlocal injected
        result = await original_run(fn, *args, **kwargs)
        if fn.__name__ == "_conditional_transition_sync" and not injected:
            injected = True
            raise disconnect_error
        return result

    db_provider.run = lose_ack_after_run
    result = await table.conditional_transition(
        "transition-ack-lost",
        expected_state="CREATING",
        new_state="LOCAL_READY",
    )

    assert injected
    assert result is not None
    assert result["state"] == "LOCAL_READY"


async def test_transition_unknown_result_accepts_a_proven_monotonic_successor(db_provider):
    table = SnapshotTable(db_provider)
    await table.create(snapshot_id="transition-progressed", kind="BASE_IMAGE", created_at=UTC_NOW)
    original_run = db_provider.run
    injected = False

    async def commit_then_advance_before_losing_ack(fn, *args, **kwargs):
        nonlocal injected
        result = await original_run(fn, *args, **kwargs)
        if fn.__name__ == "_conditional_transition_sync" and not injected:
            injected = True
            await original_run(
                table._conditional_transition_sync,
                "transition-progressed",
                "LOCAL_READY",
                {"state": "REMOTE_READY", "spec": {"committed": True}, "remote_ready_at": UTC_NOW},
            )
            raise OperationalError("commit", {}, ConnectionError("ack lost"))
        return result

    db_provider.run = commit_then_advance_before_losing_ack
    result = await table.conditional_transition(
        "transition-progressed",
        expected_state="CREATING",
        new_state="LOCAL_READY",
        acceptable_replay_states={"REMOTE_READY"},
    )

    assert injected
    assert result is not None
    assert result["state"] == "REMOTE_READY"


def test_snapshot_timestamp_columns_are_timezone_aware():
    for column_name in ("created_at", "remote_ready_at", "deleted_at"):
        assert RockSnapshotRecord.__table__.columns[column_name].type.timezone is True
