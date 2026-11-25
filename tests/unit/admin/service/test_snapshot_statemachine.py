import datetime

import pytest

import rock.admin.core.snapshot_table as snapshot_table_module
from rock.admin.core.snapshot_table import (
    BaseImageResolutionIdentity,
    SnapshotIdentityConflict,
    SnapshotTable,
)
from rock.admin.service.snapshot_statemachine import (
    MAX_SNAPSHOT_SPEC_BYTES,
    InvalidSnapshotTransition,
    SnapshotStateMachine,
)

UTC_NOW = datetime.datetime(2026, 8, 22, 8, 0, tzinfo=datetime.timezone.utc)
STATES = ("CREATING", "LOCAL_READY", "REMOTE_READY", "FAILED", "DELETED")
EVENTS = ("local_ready", "remote_ready", "fail", "delete")
LEGAL_TRANSITIONS = {
    ("CREATING", "local_ready"),
    ("CREATING", "fail"),
    ("LOCAL_READY", "remote_ready"),
    ("LOCAL_READY", "fail"),
    ("REMOTE_READY", "delete"),
    ("FAILED", "delete"),
}
ILLEGAL_TRANSITIONS = tuple(
    (state, event) for state in STATES for event in EVENTS if (state, event) not in LEGAL_TRANSITIONS
)


def full_spec(snapshot_id: str = "snapshot-1") -> dict:
    return {"backend": {"identity": snapshot_id, "objects": ["opaque-reference"]}, "optional": None}


async def _machine(table: SnapshotTable, snapshot_id: str) -> SnapshotStateMachine:
    record = await table.get(snapshot_id)
    assert record is not None
    return await SnapshotStateMachine.from_record(record, table, clock=lambda: UTC_NOW)


async def _create_full(table: SnapshotTable, snapshot_id: str) -> dict:
    return await table.create(
        snapshot_id=snapshot_id,
        kind="FULL",
        source_sandbox_id=f"source-{snapshot_id}",
        created_at=UTC_NOW,
    )


async def _advance_to(table: SnapshotTable, snapshot_id: str, state: str) -> dict:
    record = await _create_full(table, snapshot_id)
    if state == "CREATING":
        return record
    if state in {"LOCAL_READY", "REMOTE_READY"}:
        record = await (await _machine(table, snapshot_id)).send("local_ready")
        if state == "LOCAL_READY":
            return record
        return await (await _machine(table, snapshot_id)).send("remote_ready", spec=full_spec(snapshot_id))
    record = await (await _machine(table, snapshot_id)).send("fail")
    if state == "FAILED":
        return record
    assert state == "DELETED"
    return await (await _machine(table, snapshot_id)).send("delete")


@pytest.mark.parametrize("state", STATES)
async def test_restores_every_durable_state(db_provider, state) -> None:
    table = SnapshotTable(db_provider)
    record = await _advance_to(table, f"restore-{state.lower()}", state)

    machine = await SnapshotStateMachine.from_record(record, table)

    assert machine.current_state_value == state
    assert machine.snapshot == record


async def test_unknown_durable_state_fails_closed(db_provider) -> None:
    table = SnapshotTable(db_provider)
    record = await _create_full(table, "unknown-state")

    with pytest.raises(InvalidSnapshotTransition, match="unknown durable"):
        await SnapshotStateMachine.from_record({**record, "state": "BOGUS"}, table)


@pytest.mark.parametrize(
    ("source_state", "event", "target_state"),
    [
        ("CREATING", "local_ready", "LOCAL_READY"),
        ("CREATING", "fail", "FAILED"),
        ("LOCAL_READY", "remote_ready", "REMOTE_READY"),
        ("LOCAL_READY", "fail", "FAILED"),
        ("REMOTE_READY", "delete", "DELETED"),
        ("FAILED", "delete", "DELETED"),
    ],
)
async def test_legal_state_event_matrix(db_provider, source_state, event, target_state) -> None:
    table = SnapshotTable(db_provider)
    snapshot_id = f"legal-{source_state.lower()}-{event}"
    record = await _advance_to(table, snapshot_id, source_state)
    machine = await SnapshotStateMachine.from_record(record, table, clock=lambda: UTC_NOW)
    kwargs = {"spec": full_spec(snapshot_id)} if event == "remote_ready" else {}

    transitioned = await machine.send(event, **kwargs)

    assert transitioned["state"] == target_state
    assert await table.get(snapshot_id) == transitioned


@pytest.mark.parametrize(("state", "event"), ILLEGAL_TRANSITIONS)
async def test_every_illegal_state_event_pair_fails_closed(db_provider, state, event) -> None:
    table = SnapshotTable(db_provider)
    snapshot_id = f"illegal-{state.lower()}-{event}"
    record = await _advance_to(table, snapshot_id, state)
    machine = await SnapshotStateMachine.from_record(record, table)
    kwargs = {"spec": full_spec(snapshot_id)} if event == "remote_ready" else {}

    with pytest.raises(InvalidSnapshotTransition, match="not allowed"):
        await machine.send(event, **kwargs)
    assert await table.get(snapshot_id) == record


async def test_remote_ready_requires_spec_and_commits_spec_and_timestamp_atomically(db_provider) -> None:
    table = SnapshotTable(db_provider)
    await _advance_to(table, "snapshot-atomic", "LOCAL_READY")

    with pytest.raises(InvalidSnapshotTransition, match="requires a snapshot spec"):
        await (await _machine(table, "snapshot-atomic")).send("remote_ready")
    spec = full_spec("snapshot-atomic")
    remote = await (await _machine(table, "snapshot-atomic")).send("remote_ready", spec=spec)

    assert remote["spec"] == spec
    assert remote["remote_ready_at"] == UTC_NOW


@pytest.mark.parametrize("spec", [{}, {"backend_v2": {"objects": [None, "opaque"], "标签": "快照"}}])
async def test_remote_ready_preserves_opaque_backend_descriptor(db_provider, spec) -> None:
    table = SnapshotTable(db_provider)
    await _advance_to(table, "opaque-spec", "LOCAL_READY")
    remote = await (await _machine(table, "opaque-spec")).send("remote_ready", spec=spec)
    assert remote["spec"] == spec


@pytest.mark.parametrize(
    "spec",
    [[], {"value": float("nan")}, {"value": object()}, {"value": "x" * MAX_SNAPSHOT_SPEC_BYTES}],
)
async def test_remote_ready_rejects_non_json_or_oversized_descriptors(db_provider, spec) -> None:
    table = SnapshotTable(db_provider)
    local = await _advance_to(table, "invalid-json", "LOCAL_READY")
    with pytest.raises(InvalidSnapshotTransition, match="snapshot spec"):
        await (await _machine(table, "invalid-json")).send("remote_ready", spec=spec)
    assert await table.get("invalid-json") == local


async def test_remote_ready_commits_summary_without_replacing_frozen_resources(db_provider) -> None:
    table = SnapshotTable(db_provider)
    await table.create(
        snapshot_id="summary", kind="FULL", source_sandbox_id="source", created_at=UTC_NOW,
        resource_spec={"vcpus": 2, "memory_mib": 128, "disk": "1G"},
    )
    await (await _machine(table, "summary")).send("local_ready")
    spec = full_spec("summary")
    remote = await (await _machine(table, "summary")).send(
        "remote_ready", spec=spec, memory_bytes=134217728, rootfs_bytes=67108864,
    )
    assert remote["spec"] == spec
    assert remote["resource_spec"] == {
        "vcpus": 2, "memory_mib": 128, "disk": "1G",
        "memory_bytes": 134217728, "rootfs_bytes": 67108864,
    }
    assert remote["remote_ready_at"] == UTC_NOW


async def test_base_image_remote_ready_accepts_opaque_backend_descriptor(db_provider) -> None:
    table = SnapshotTable(db_provider)
    snapshot_id = "base-catalog-exact"
    claim = await table.claim_base_image(
        snapshot_id=snapshot_id,
        resolution=BaseImageResolutionIdentity(
            oci_ref="registry.example/base:stable",
            oci_digest="sha256:" + "d" * 64,
            oci_platform="linux/amd64",
            base_image_builder_version="test-builder-v1",
            base_image_build_key="sha256:" + "f" * 64,
            source_worker_ip="10.0.0.9",
        ),
        created_at=UTC_NOW,
    )
    local = await (await SnapshotStateMachine.from_record(claim.snapshot, table)).send("local_ready")

    remote = await (
        await SnapshotStateMachine.from_record(local, table, clock=lambda: UTC_NOW)
    ).send("remote_ready", spec=full_spec(snapshot_id))

    assert remote["state"] == "REMOTE_READY"


async def test_delete_timestamp_uses_injected_clock(db_provider) -> None:
    table = SnapshotTable(db_provider)
    await _advance_to(table, "delete-clock", "FAILED")

    deleted = await (await _machine(table, "delete-clock")).send("delete")

    assert deleted["deleted_at"] == UTC_NOW


class _CasTable:
    def __init__(self, observed: dict, *, transition_result=None) -> None:
        self.observed = observed
        self.transition_result = transition_result
        self.calls: list[dict] = []

    async def conditional_transition(self, _snapshot_id, **kwargs):
        self.calls.append(kwargs)
        return self.transition_result

    async def get(self, _snapshot_id):
        return dict(self.observed)


async def test_identical_cas_replay_returns_the_durable_row() -> None:
    local = {
        "snapshot_id": "cas-replay",
        "kind": "FULL",
        "format_version": 1,
        "state": "LOCAL_READY",
        "spec": None,
    }
    remote = {**local, "state": "REMOTE_READY", "spec": full_spec("cas-replay")}
    table = _CasTable(remote)
    machine = await SnapshotStateMachine.from_record(local, table)  # type: ignore[arg-type]

    result = await machine.send("remote_ready", spec=full_spec("cas-replay"))

    assert result == remote
    assert table.calls[0]["acceptable_replay_states"] == frozenset({"DELETED"})


async def test_different_immutable_replay_values_raise_identity_conflict() -> None:
    local = {
        "snapshot_id": "cas-conflict",
        "kind": "FULL",
        "format_version": 1,
        "state": "LOCAL_READY",
        "spec": None,
    }
    other_spec = full_spec("cas-conflict")
    other_spec["backend"]["objects"] = ["different-reference"]
    table = _CasTable({**local, "state": "REMOTE_READY", "spec": other_spec})
    machine = await SnapshotStateMachine.from_record(local, table)  # type: ignore[arg-type]

    with pytest.raises(SnapshotIdentityConflict, match="different values"):
        await machine.send("remote_ready", spec=full_spec("cas-conflict"))


@pytest.mark.parametrize("observed_bytes", [4096, 8192])
async def test_remote_ready_replay_includes_size_summary_identity(observed_bytes) -> None:
    local = {
        "snapshot_id": "summary-replay", "state": "LOCAL_READY", "spec": None,
        "resource_spec": {"vcpus": 2, "memory_mib": 128},
    }
    spec = full_spec("summary-replay")
    observed = {
        **local, "state": "REMOTE_READY", "spec": spec,
        "resource_spec": {**local["resource_spec"], "rootfs_bytes": observed_bytes},
    }
    table = _CasTable(observed)
    machine = await SnapshotStateMachine.from_record(local, table)  # type: ignore[arg-type]
    if observed_bytes == 4096:
        assert await machine.send("remote_ready", spec=spec, rootfs_bytes=4096) == observed
    else:
        with pytest.raises(SnapshotIdentityConflict, match="different values"):
            await machine.send("remote_ready", spec=spec, rootfs_bytes=4096)


async def test_unrelated_cas_loser_is_not_reported_as_success() -> None:
    creating = {
        "snapshot_id": "cas-loser",
        "kind": "FULL",
        "format_version": 1,
        "state": "CREATING",
        "spec": None,
    }
    table = _CasTable({**creating, "state": "FAILED"})
    machine = await SnapshotStateMachine.from_record(creating, table)  # type: ignore[arg-type]

    with pytest.raises(InvalidSnapshotTransition, match="concurrent CAS"):
        await machine.send("local_ready")


def test_replay_states_are_descendants_in_the_statechart_without_table_policy() -> None:
    adjacency = {
        state.value: {transition.target.value for transition in state.transitions}
        for state in SnapshotStateMachine.states
    }
    for target in ("LOCAL_READY", "REMOTE_READY", "FAILED", "DELETED"):
        descendants = set()
        pending = list(adjacency[target])
        while pending:
            state = pending.pop()
            if state not in descendants:
                descendants.add(state)
                pending.extend(adjacency[state])
        assert SnapshotStateMachine._safe_replay_states(target) <= descendants
    assert not hasattr(snapshot_table_module, "_STATE_DESCENDANTS")
