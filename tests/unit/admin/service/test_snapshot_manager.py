from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rock.admin.core.snapshot_table import SnapshotIdentityConflict
from rock.admin.proto.snapshot import CreateSnapshotRequest, ForkSnapshotRequest, StartFromSnapshotRequest
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotManager, SnapshotResultUnknown
from rock.admin.snapd.client import SnapdResultUnknown
from rock.admin.snapd.protocol import MAX_FORK_CHILDREN
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.snapd import (
    ForkOperationResult,
    OperatorResultCode,
    OperatorTarget,
    SnapdOperator,
    SnapshotOperationResult,
)
from rock.sandbox.snapshot_types import ForkChildLaunchPlan, SnapshotSource


class _Catalog:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    async def create_full_snapshot_attempt(
        self,
        *,
        snapshot_id,
        source_sandbox_id,
        source_worker_ip,
        resource_spec,
        created_at,
    ):
        if snapshot_id in self.records:
            raise SnapshotIdentityConflict("snapshot ID already exists")
        record = {
            "snapshot_id": snapshot_id,
            "kind": "FULL",
            "source_sandbox_id": source_sandbox_id,
            "source_worker_ip": source_worker_ip,
            "format_version": 1,
            "resource_spec": deepcopy(resource_spec),
            "state": "CREATING",
        }
        self.records[snapshot_id] = record
        return dict(record)

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
        record = self.records[snapshot_id]
        if record["state"] == new_state or record["state"] in acceptable_replay_states:
            for key, value in (replay_identity_values or {}).items():
                if record.get(key) != value:
                    raise ValueError("snapshot transition identity conflict")
            return dict(record)
        if record["state"] != expected_state:
            return None
        record["state"] = new_state
        record.update(values or {})
        return dict(record)

    async def get(self, snapshot_id):
        record = self.records.get(snapshot_id)
        return None if record is None else dict(record)


class _SnapshotOperator:
    def __init__(self) -> None:
        self.capture_result = SnapshotOperationResult("fixture", OperatorResultCode.ACCEPTED, "LOCAL_READY")
        self.fork_result = ForkOperationResult("fixture", OperatorResultCode.ACCEPTED, "LOCAL_READY")
        self.capture_requests = []
        self.fork_requests = []

    async def resolve_source(self, source):
        return source

    async def capture_snapshot(self, source, request):
        self.capture_requests.append((source, request))
        return replace(self.capture_result, snapshot_id=request.snapshot_id)

    async def fork_snapshot(self, source, request):
        self.fork_requests.append((source, request))
        result = self.fork_result
        if not result.payload and result.code is not OperatorResultCode.RESULT_UNKNOWN:
            payload = {"children": [{"sandbox_id": child.sandbox_id, "state": "RUNNING"} for child in request.children]}
            return replace(result, snapshot_id=request.snapshot_id, payload=payload)
        return replace(result, snapshot_id=request.snapshot_id)


class _SandboxLifecycle:
    async def list_active_snapshot_users(self, _snapshot_id):
        return ()

    async def start_from_snapshot(self, snapshot, request, **kwargs):
        return {"sandbox_id": request.sandbox_id, "source_snapshot_id": snapshot.snapshot_id, "state": "PENDING"}


def _source(*, resource_spec: dict[str, int] | None = None) -> SnapshotSource:
    return SnapshotSource(
        sandbox_id="source",
        sandbox_namespace="tenant-a",
        target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1"),
        resource_spec=resource_spec if resource_spec is not None else {"vcpus": 2, "memory_mib": 256},
    )


def _manager(catalog: _Catalog, operator: _SnapshotOperator) -> SnapshotManager:
    return SnapshotManager(
        catalog,  # type: ignore[arg-type]
        operator,  # type: ignore[arg-type]
        _SandboxLifecycle(),  # type: ignore[arg-type]
    )


def _fork_request(
    *,
    count: int = 2,
    resource_spec: dict[str, int] | None = None,
) -> ForkSnapshotRequest:
    return ForkSnapshotRequest(
        snapshot_id="snapshot-fork",
        count=count,
        target_sandbox_ids=[f"child-{index}" for index in range(count)],
        resource_spec=resource_spec if resource_spec is not None else {},
    )


def _assert_random_sandbox_id(value: str) -> None:
    assert len(value) == 32
    parsed = uuid.UUID(hex=value)
    assert parsed.hex == value
    assert parsed.version == 4


@pytest.mark.asyncio
async def test_default_capture_ids_are_new_snap_prefixed_uuids() -> None:
    manager = _manager(_Catalog(), _SnapshotOperator())
    first = await manager.capture_snapshot(_source(), CreateSnapshotRequest())
    second = await manager.capture_snapshot(_source(), CreateSnapshotRequest())

    assert first["snapshot_id"] != second["snapshot_id"]
    for record in (first, second):
        assert record["snapshot_id"].startswith("snap-")
        _assert_random_sandbox_id(record["snapshot_id"][5:])


@pytest.mark.asyncio
async def test_default_fork_uses_random_snapshot_and_unprefixed_children() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    request = ForkSnapshotRequest(count=2)
    first = await manager.prepare_fork(_source(), request, child_timeout_seconds=None)
    second = await manager.prepare_fork(_source(), request, child_timeout_seconds=None)

    assert first.snapshot_id != second.snapshot_id
    assert set(first.target_sandbox_ids).isdisjoint(second.target_sandbox_ids)
    for preparation in (first, second):
        assert preparation.snapshot_id.startswith("snap-")
        _assert_random_sandbox_id(preparation.snapshot_id[5:])
        for sandbox_id in preparation.target_sandbox_ids:
            _assert_random_sandbox_id(sandbox_id)
        assert "operation_identity" not in catalog.records[preparation.snapshot_id]
        assert "runtime_epoch" not in catalog.records[preparation.snapshot_id]


@pytest.mark.asyncio
async def test_generated_child_collision_is_left_to_individual_inserts(monkeypatch) -> None:
    repeated = uuid.UUID("00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(uuid, "uuid4", lambda: repeated)
    manager = _manager(_Catalog(), _SnapshotOperator())

    preparation = await manager.prepare_fork(
        _source(), ForkSnapshotRequest(snapshot_id="collision-cut", count=2), child_timeout_seconds=None,
    )

    assert preparation.target_sandbox_ids == (repeated.hex, repeated.hex)


@pytest.mark.asyncio
async def test_same_fork_id_has_one_winner_across_managers_without_a_plan_lock() -> None:
    catalog = _Catalog()

    class YieldingOperator(_SnapshotOperator):
        async def resolve_source(self, source):
            await asyncio.sleep(0)
            return await super().resolve_source(source)

    first_manager = _manager(catalog, YieldingOperator())
    second_manager = _manager(catalog, YieldingOperator())
    request = ForkSnapshotRequest(snapshot_id="explicit-fork", count=2)
    results = await asyncio.gather(
        first_manager.prepare_fork(_source(), request, child_timeout_seconds=15),
        second_manager.prepare_fork(_source(), request, child_timeout_seconds=15),
        return_exceptions=True,
    )

    winners = [result for result in results if not isinstance(result, Exception)]
    assert len(winners) == 1
    assert sum(isinstance(result, SnapshotConflict) for result in results) == 1
    assert len(catalog.records) == 1
    assert catalog.records[request.snapshot_id]["state"] == "CREATING"
    for sandbox_id in winners[0].target_sandbox_ids:
        _assert_random_sandbox_id(sandbox_id)

    with pytest.raises(SnapshotConflict, match="already exists"):
        await second_manager.prepare_fork(
            _source(),
            request.model_copy(update={"target_sandbox_ids": list(winners[0].target_sandbox_ids)}),
            child_timeout_seconds=15,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes, timeout", [
    ({}, None),
    ({"count": 3}, None),
    ({"resource_spec": {"vcpus": 8, "memory_mib": 1024}}, None),
    ({"target_sandbox_ids": ["other-child-1", "other-child-2"]}, None),
    ({}, 30),
])
async def test_duplicate_fork_id_rejected_with_same_or_changed_body_without_failing_original(changes, timeout) -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    request = ForkSnapshotRequest(snapshot_id="explicit-fork", count=2)
    first = await manager.prepare_fork(_source(), request, child_timeout_seconds=None)
    original = dict(catalog.records[first.snapshot_id])

    with pytest.raises(SnapshotConflict):
        await manager.prepare_fork(_source(), request.model_copy(update=changes), child_timeout_seconds=timeout)

    assert catalog.records[first.snapshot_id] == original


@pytest.mark.asyncio
async def test_restore_allocates_one_unprefixed_uuid_and_preserves_explicit_id() -> None:
    catalog = _Catalog()
    catalog.records["ready"] = {
        "snapshot_id": "ready", "kind": "FULL", "state": "REMOTE_READY", "spec": {"ready": True},
        "format_version": 1,
        "resource_spec": {"vcpus": 2, "memory_mib": 256},
    }
    manager = _manager(catalog, _SnapshotOperator())
    first = await manager.start_async("ready", StartFromSnapshotRequest())
    second = await manager.start_async("ready", StartFromSnapshotRequest())
    explicit = await manager.start_async("ready", StartFromSnapshotRequest(sandbox_id="my-sandbox"))

    assert first["sandbox_id"] != second["sandbox_id"]
    _assert_random_sandbox_id(first["sandbox_id"])
    _assert_random_sandbox_id(second["sandbox_id"])
    assert explicit["sandbox_id"] == "my-sandbox"


@pytest.mark.asyncio
async def test_capture_uses_snapd_epoch_without_sequence_number() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)

    record = await manager.capture_snapshot(
        _source(),
        CreateSnapshotRequest(snapshot_id="snapshot-authoritative"),
    )

    sent = operator.capture_requests[0][1]

    assert "generation" not in sent.model_dump()
    assert "generation" not in record
    assert record["state"] == "LOCAL_READY"
    assert record["source_worker_ip"] == "10.0.0.1"
    assert "runtime_epoch" not in record
    assert "operation_identity" not in record


@pytest.mark.asyncio
async def test_capture_persists_resolved_owner_before_dispatch() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    source = replace(_source(), target=OperatorTarget(node_id="resolved-node", host_ip="10.0.0.42"))
    operator.resolve_source = AsyncMock(return_value=source)

    async def capture(resolved_source, request):
        assert resolved_source == source
        assert catalog.records[request.snapshot_id]["source_worker_ip"] == "10.0.0.42"
        assert catalog.records[request.snapshot_id]["state"] == "CREATING"
        return replace(operator.capture_result, snapshot_id=request.snapshot_id)

    operator.capture_snapshot = AsyncMock(side_effect=capture)
    record = await _manager(catalog, operator).capture_snapshot(
        _source(), CreateSnapshotRequest(snapshot_id="owner-before-dispatch"),
    )

    assert record["source_worker_ip"] == "10.0.0.42"
    operator.capture_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_fork_phases_use_the_in_memory_request_plan() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    preparation = await manager.prepare_fork(
        _source(),
        _fork_request(),
        child_timeout_seconds=37,
    )
    plans = tuple(
        ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
        for sandbox_id in preparation.target_sandbox_ids
    )

    result = await manager.execute_fork(preparation, plans)
    record = await manager.complete_fork(preparation, result)

    sent = operator.fork_requests[0][1]

    assert "generation" not in sent.model_dump()
    assert "failure_policy" not in sent.model_dump()

    assert [child.sandbox_id for child in result.children] == ["child-0", "child-1"]
    assert record["state"] == "LOCAL_READY"
    assert record["source_worker_ip"] == "10.0.0.1"
    assert preparation.child_timeout_seconds == 37
    assert "operation_identity" not in record
    assert "runtime_epoch" not in record


@pytest.mark.asyncio
async def test_fork_without_resource_spec_inherits_source_geometry() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    request = _fork_request(resource_spec={})

    preparation = await manager.prepare_fork(
        _source(resource_spec={"vcpus": 3, "memory_mib": 768}),
        request,
        child_timeout_seconds=None,
    )

    assert preparation.resource_spec == {"vcpus": 3, "memory_mib": 768}


@pytest.mark.asyncio
async def test_fork_explicit_resource_spec_cannot_resize_captured_geometry() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())

    with pytest.raises(SnapshotConflict, match="CPU count cannot be changed"):
        await manager.prepare_fork(
            _source(resource_spec={"vcpus": 3, "memory_mib": 768}),
            _fork_request(resource_spec={"vcpus": 1, "memory_mib": 128}),
            child_timeout_seconds=None,
        )
    assert catalog.records == {}


@pytest.mark.asyncio
async def test_capture_freezes_integer_vm_geometry_before_dispatch() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    resources = {"vcpus": 1.5, "memory_mib": 768, "disk": "4G"}
    record = await manager.capture_snapshot(_source(resource_spec=resources), CreateSnapshotRequest())
    resources["memory_mib"] = 1024
    assert record["resource_spec"] == {"vcpus": 2, "memory_mib": 768, "disk": "4G"}
    assert operator.capture_requests[0][0].resource_spec == record["resource_spec"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [{}, {"vcpus": 3}, {"memory_mib": 768}, {"cpus": 3}])
async def test_full_restore_inherits_frozen_geometry_and_rebinds_deployment_defaults(requested) -> None:
    catalog = _Catalog()
    frozen = {"vcpus": 3, "memory_mib": 768, "disk": "4G"}
    catalog.records["ready"] = {
        "snapshot_id": "ready", "kind": "FULL", "state": "REMOTE_READY", "spec": {},
        "format_version": 1, "resource_spec": frozen,
    }
    manager = _manager(catalog, _SnapshotOperator())
    manager._sandbox_lifecycle.start_from_snapshot = AsyncMock(return_value={})
    request = StartFromSnapshotRequest(sandbox_id="restored", resource_spec=requested)
    config = DockerDeploymentConfig(container_name="restored")
    await manager.start_async("ready", request, deployment_config=config)
    call = manager._sandbox_lifecycle.start_from_snapshot.call_args
    assert call.args[0].resource_spec == frozen
    assert call.args[1].resource_spec == frozen
    assert call.kwargs["deployment_config"].cpus == 3
    assert call.kwargs["deployment_config"].memory == "768M"
    assert call.kwargs["deployment_config"].disk == "4G"
    assert request.resource_spec == requested
    assert config.cpus == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [{"vcpus": 1}, {"cpus": 1}, {"memory_mib": 512}])
async def test_full_restore_rejects_conflicting_geometry_before_sandbox_admission(requested) -> None:
    catalog = _Catalog()
    catalog.records["ready"] = {
        "snapshot_id": "ready", "kind": "FULL", "state": "REMOTE_READY", "spec": {},
        "format_version": 1, "resource_spec": {"vcpus": 3, "memory_mib": 768},
    }
    manager = _manager(catalog, _SnapshotOperator())
    manager._sandbox_lifecycle.start_from_snapshot = AsyncMock()
    with pytest.raises(SnapshotConflict, match="cannot be changed"):
        await manager.start_async("ready", StartFromSnapshotRequest(resource_spec=requested))
    manager._sandbox_lifecycle.start_from_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_image_restore_still_uses_requested_cold_boot_resources() -> None:
    catalog = _Catalog()
    catalog.records["base"] = {
        "snapshot_id": "base", "kind": "BASE_IMAGE", "state": "REMOTE_READY", "spec": {}, "format_version": 1,
    }
    manager = _manager(catalog, _SnapshotOperator())
    manager._sandbox_lifecycle.start_from_snapshot = AsyncMock(return_value={})
    resources = {"vcpus": 5, "memory_mib": 1536}
    await manager.start_async("base", StartFromSnapshotRequest(resource_spec=resources))
    assert manager._sandbox_lifecycle.start_from_snapshot.call_args.args[1].resource_spec == resources


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", [None, "stopped", "archived", "deleted"])
@pytest.mark.parametrize("snapshot_state", ["CREATING", "LOCAL_READY"])
async def test_delete_abandoned_full_uses_existing_fail_then_delete(snapshot_state, source_state) -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    await manager._prepare_cut_attempt(_source(), snapshot_id="abandoned")
    catalog.records["abandoned"]["state"] = snapshot_state
    manager._sandbox_lifecycle.get_product_view = AsyncMock(
        return_value=SimpleNamespace(state=source_state) if source_state else None,
    )
    operator.get_snapshot_status = AsyncMock(return_value=SnapshotOperationResult(
        "abandoned", OperatorResultCode.NOT_FOUND, "FAILED",
    ))
    record = await manager.delete_snapshot("abandoned")
    assert record["state"] == "DELETED"
    assert record["deleted_at"] is not None
    operator.get_snapshot_status.assert_awaited_once_with("abandoned", source_worker_ip="10.0.0.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["running", "pending", "future-state"])
async def test_delete_unfinished_full_keeps_a_live_source_attempt(source_state) -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    await manager._prepare_cut_attempt(_source(), snapshot_id="in-flight")
    manager._sandbox_lifecycle.get_product_view = AsyncMock(return_value=SimpleNamespace(state=source_state))
    with pytest.raises(SnapshotConflict, match="end the source"):
        await manager.delete_snapshot("in-flight")
    assert catalog.records["in-flight"]["state"] == "CREATING"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,error_type", [
    (OperatorResultCode.ACCEPTED, SnapshotConflict),
    (OperatorResultCode.FAILED, SnapshotConflict),
    (OperatorResultCode.RESULT_UNKNOWN, SnapshotResultUnknown),
])
async def test_delete_unfinished_full_requires_explicit_worker_absence(code, error_type) -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    await manager._prepare_cut_attempt(_source(), snapshot_id="unknown")
    manager._sandbox_lifecycle.get_product_view = AsyncMock(return_value=SimpleNamespace(state="stopped"))
    operator.get_snapshot_status = AsyncMock(return_value=SnapshotOperationResult("unknown", code, "CREATING"))
    with pytest.raises(error_type):
        await manager.delete_snapshot("unknown")
    assert catalog.records["unknown"]["state"] == "CREATING"


@pytest.mark.asyncio
async def test_duplicate_fork_id_is_rejected_after_source_resources_change() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    request = _fork_request(resource_spec={})
    await manager.prepare_fork(
        _source(resource_spec={"vcpus": 1, "memory_mib": 256}),
        request,
        child_timeout_seconds=None,
    )

    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.prepare_fork(
            _source(resource_spec={"vcpus": 1, "memory_mib": 512}),
            request,
            child_timeout_seconds=None,
        )


@pytest.mark.asyncio
async def test_fork_result_unknown_keeps_catalog_creating() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    operator.fork_result = ForkOperationResult(
        "fixture", OperatorResultCode.RESULT_UNKNOWN, "CREATING", last_error="lost reply"
    )
    manager = _manager(catalog, operator)
    preparation = await manager.prepare_fork(
        _source(), _fork_request(), child_timeout_seconds=None
    )
    plans = tuple(
        ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
        for sandbox_id in preparation.target_sandbox_ids
    )

    result = await manager.execute_fork(preparation, plans)

    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert result.last_error == "lost reply"
    assert [child.sandbox_id for child in result.children] == list(preparation.target_sandbox_ids)
    assert all(child.state == "PENDING" for child in result.children)
    assert catalog.records["snapshot-fork"]["state"] == "CREATING"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_pending_observation", [False, True])
async def test_unknown_fork_preserves_admission_failure_and_pending_placement(with_pending_observation) -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    children = [{"sandbox_id": "child-0", "state": "FAILED", "last_error": "actor reservation failed"}]
    placement = {"node_id": "node-a", "host_ip": "10.0.0.1", "host_name": "ray-a"}
    if with_pending_observation:
        children.append({"sandbox_id": "child-1", "state": "PENDING", "placement": placement})
    operator.fork_result = ForkOperationResult(
        "fixture", OperatorResultCode.RESULT_UNKNOWN, "CREATING",
        payload={"children": children}, last_error="lost shared fork reply",
    )
    manager = _manager(catalog, operator)
    preparation = await manager.prepare_fork(_source(), _fork_request(), child_timeout_seconds=None)

    result = await manager.execute_fork(preparation, tuple(
        ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
        for sandbox_id in preparation.target_sandbox_ids
    ))

    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert [child.sandbox_id for child in result.children] == ["child-0", "child-1"]
    assert [child.state for child in result.children] == ["FAILED", "PENDING"]
    assert result.children[0].last_error == "actor reservation failed"
    assert result.children[1].guest_endpoint is None
    if with_pending_observation:
        assert result.children[1].target == OperatorTarget(**placement)
    assert catalog.records["snapshot-fork"]["state"] == "CREATING"


@pytest.mark.asyncio
async def test_fork_rejects_operator_child_reordering() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    operator.fork_result = ForkOperationResult(
        "fixture",
        OperatorResultCode.ACCEPTED,
        "LOCAL_READY",
        payload={
            "children": [
                {"sandbox_id": "child-1", "state": "RUNNING"},
                {"sandbox_id": "child-0", "state": "RUNNING"},
            ]
        },
    )
    manager = _manager(catalog, operator)
    preparation = await manager.prepare_fork(
        _source(), _fork_request(), child_timeout_seconds=None
    )
    plans = tuple(
        ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
        for sandbox_id in preparation.target_sandbox_ids
    )

    with pytest.raises(SnapshotConflict, match="invalid fork child set or order"):
        await manager.execute_fork(preparation, plans)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_child_state", ["RUNNING", "FAILED", "PENDING"])
async def test_child_failures_do_not_fail_a_successfully_captured_snapshot(first_child_state) -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    operator.fork_result = ForkOperationResult(
        "fixture",
        OperatorResultCode.ACCEPTED,
        "LOCAL_READY",
        payload={
            "children": [
                {"sandbox_id": "child-0", "state": first_child_state},
                {"sandbox_id": "child-1", "state": "FAILED"},
            ]
        },
    )
    manager = _manager(catalog, operator)
    preparation = await manager.prepare_fork(
        _source(), _fork_request(), child_timeout_seconds=None
    )
    result = await manager.execute_fork(
        preparation,
        tuple(
            ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
            for sandbox_id in preparation.target_sandbox_ids
        ),
    )

    record = await manager.complete_fork(preparation, result)

    assert record["state"] == "LOCAL_READY"
    assert [child.state for child in result.children] == [first_child_state, "FAILED"]


@pytest.mark.asyncio
async def test_fork_limit_rejects_oversize_before_catalog_mutation() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())

    with pytest.raises(ValueError, match="less than or equal"):
        await manager.prepare_fork(
            _source(),
            _fork_request(count=MAX_FORK_CHILDREN + 1),
            child_timeout_seconds=None,
        )

    assert catalog.records == {}


@pytest.mark.asyncio
async def test_concurrent_captures_reach_snapd_and_only_rejected_attempt_fails() -> None:
    catalog = _Catalog()
    both_dispatched = asyncio.Event()

    class ConcurrentOperator(_SnapshotOperator):
        async def capture_snapshot(self, source, request):
            self.capture_requests.append((source, request))
            if len(self.capture_requests) == 2:
                assert {row["state"] for row in catalog.records.values()} == {"CREATING"}
                both_dispatched.set()
            await both_dispatched.wait()
            if request.snapshot_id == "loser":
                return SnapshotOperationResult(
                    request.snapshot_id, OperatorResultCode.IDENTITY_CONFLICT,
                    "FAILED", last_error="source sandbox is busy; request was not submitted",
                )
            return SnapshotOperationResult(request.snapshot_id, OperatorResultCode.ACCEPTED, "LOCAL_READY")

    operator = ConcurrentOperator()
    managers = [_manager(catalog, operator), _manager(catalog, operator)]
    results = await asyncio.gather(*(
        manager.capture_snapshot(_source(), CreateSnapshotRequest(snapshot_id=identity))
        for manager, identity in zip(managers, ("winner", "loser"), strict=True)
    ), return_exceptions=True)
    assert results[0]["state"] == "LOCAL_READY"
    assert isinstance(results[1], SnapshotConflict)
    assert catalog.records["loser"]["state"] == "FAILED"
    assert {request.snapshot_id for _, request in operator.capture_requests} == {"winner", "loser"}


@pytest.mark.asyncio
async def test_concurrent_forks_reach_snapd_and_only_rejected_attempt_fails() -> None:
    catalog = _Catalog()
    both_dispatched = asyncio.Event()

    class ConcurrentOperator(_SnapshotOperator):
        async def fork_snapshot(self, source, request):
            self.fork_requests.append((source, request))
            if len(self.fork_requests) == 2:
                assert {row["state"] for row in catalog.records.values()} == {"CREATING"}
                both_dispatched.set()
            await both_dispatched.wait()
            if request.snapshot_id == "loser":
                return ForkOperationResult(
                    request.snapshot_id, OperatorResultCode.IDENTITY_CONFLICT,
                    "FAILED", last_error="source sandbox is busy; request was not submitted",
                )
            return ForkOperationResult(
                request.snapshot_id, OperatorResultCode.ACCEPTED, "LOCAL_READY",
                payload={"children": [{"sandbox_id": child.sandbox_id, "state": "RUNNING"}
                                      for child in request.children]},
            )

    operator = ConcurrentOperator()
    managers = [_manager(catalog, operator), _manager(catalog, operator)]
    preparations = await asyncio.gather(*(
        manager.prepare_fork(
            _source(), ForkSnapshotRequest(snapshot_id=identity, count=1), child_timeout_seconds=None,
        )
        for manager, identity in zip(managers, ("winner", "loser"), strict=True)
    ))
    results = await asyncio.gather(*(
        manager.execute_fork(preparation, (
            ForkChildLaunchPlan(preparation.target_sandbox_ids[0],  dict(preparation.resource_spec)),
        ))
        for manager, preparation in zip(managers, preparations, strict=True)
    ))
    for manager, preparation, result in zip(managers, preparations, results, strict=True):
        await manager.complete_fork(preparation, result)
    assert catalog.records["winner"]["state"] == "LOCAL_READY"
    assert catalog.records["loser"]["state"] == "FAILED"
    assert results[1].code is OperatorResultCode.IDENTITY_CONFLICT
    assert {request.snapshot_id for _, request in operator.fork_requests} == {"winner", "loser"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["CREATING", "LOCAL_READY", "REMOTE_READY", "FAILED", "DELETED"])
async def test_existing_capture_id_is_always_a_conflict_and_preserves_original(state) -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    catalog.records["existing"] = {"snapshot_id": "existing", "state": state, "kind": "FULL"}
    original = dict(catalog.records["existing"])

    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.capture_snapshot(_source(), CreateSnapshotRequest(snapshot_id="existing"))

    assert catalog.records["existing"] == original
    assert operator.capture_requests == []


@pytest.mark.asyncio
async def test_capture_after_restart_uses_current_epoch_without_persisting_it() -> None:
    catalog = _Catalog()

    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    for epoch in (17, 99):
        result = await manager.capture_snapshot(replace(_source()), CreateSnapshotRequest())
        assert "generation" not in result
        assert "runtime_epoch" not in result

    assert len(catalog.records) == 2


@pytest.mark.asyncio
async def test_capture_unknown_stays_creating_and_duplicate_does_not_redispatch() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    request = CreateSnapshotRequest(snapshot_id="lost-capture")
    operator.capture_result = SnapshotOperationResult(
        "fixture", OperatorResultCode.RESULT_UNKNOWN, "CREATING", last_error="lost reply"
    )
    with pytest.raises(SnapshotResultUnknown):
        await manager.capture_snapshot(_source(), request)
    assert catalog.records["lost-capture"]["state"] == "CREATING"
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.capture_snapshot(_source(), request)
    assert catalog.records["lost-capture"]["state"] == "CREATING"
    assert len(operator.capture_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capture", "fork"])
async def test_missing_source_actor_is_manager_unknown_before_creating_attempt(operation) -> None:
    catalog = _Catalog()
    operator = SnapdOperator.__new__(SnapdOperator)
    operator._find_actor = AsyncMock(return_value=None)
    manager = _manager(catalog, operator)

    with pytest.raises(SnapshotResultUnknown, match="no live execution actor") as raised:
        if operation == "capture":
            await manager.capture_snapshot(_source(), CreateSnapshotRequest(snapshot_id="missing-source"))
        else:
            await manager.prepare_fork(_source(), _fork_request(), child_timeout_seconds=None)

    assert isinstance(raised.value.__cause__, SnapdResultUnknown)
    assert catalog.records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capture", "fork"])
async def test_source_actor_lost_after_preparation_preserves_unknown_attempt(operation) -> None:
    catalog = _Catalog()
    operator = SnapdOperator.__new__(SnapdOperator)
    operator.resolve_source = AsyncMock(return_value=_source())
    operator._find_actor = AsyncMock(return_value=None)
    manager = _manager(catalog, operator)

    with pytest.raises(SnapshotResultUnknown, match="no live execution actor") as raised:
        if operation == "capture":
            snapshot_id = "lost-source"
            await manager.capture_snapshot(_source(), CreateSnapshotRequest(snapshot_id=snapshot_id))
        else:
            preparation = await manager.prepare_fork(_source(), _fork_request(), child_timeout_seconds=None)
            snapshot_id = preparation.snapshot_id
            plans = tuple(
                ForkChildLaunchPlan(sandbox_id,  dict(preparation.resource_spec))
                for sandbox_id in preparation.target_sandbox_ids
            )
            await manager.execute_fork(preparation, plans)

    assert isinstance(raised.value.__cause__, SnapdResultUnknown)
    assert catalog.records[snapshot_id]["state"] == "CREATING"
    operator._find_actor.assert_awaited_once_with("source")


@pytest.mark.asyncio
async def test_duplicate_capture_id_is_rejected_without_observing_restarted_source() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    request = CreateSnapshotRequest(snapshot_id="old-runtime")
    operator.capture_result = SnapshotOperationResult("fixture", OperatorResultCode.RESULT_UNKNOWN, "CREATING")
    with pytest.raises(SnapshotResultUnknown):
        await manager.capture_snapshot(_source(), request)

    async def must_not_observe_new_runtime(_source):
        raise AssertionError("a duplicate ID must fail before querying or dispatching to snapd")

    operator.resolve_source = must_not_observe_new_runtime
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.capture_snapshot(replace(_source()), request)
    assert catalog.records[request.snapshot_id]["state"] == "CREATING"
    assert len(operator.capture_requests) == 1


@pytest.mark.asyncio
async def test_changed_same_snapshot_request_does_not_fail_winner_or_dispatch() -> None:
    catalog = _Catalog()
    operator = _SnapshotOperator()
    manager = _manager(catalog, operator)
    await manager.prepare_fork(_source(), _fork_request(), child_timeout_seconds=None)
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.capture_snapshot(
            _source(), CreateSnapshotRequest(snapshot_id="snapshot-fork")
        )
    assert catalog.records["snapshot-fork"]["state"] == "CREATING"
    assert operator.capture_requests == []


@pytest.mark.asyncio
async def test_failure_cas_does_not_overwrite_concurrent_local_ready_winner() -> None:
    catalog = _Catalog()
    manager = _manager(catalog, _SnapshotOperator())
    await manager.prepare_fork(_source(), _fork_request(), child_timeout_seconds=None)
    original_get = catalog.get
    first_read = True

    async def publish_after_read(snapshot_id):
        nonlocal first_read
        row = await original_get(snapshot_id)
        if first_read:
            first_read = False
            catalog.records[snapshot_id]["state"] = "LOCAL_READY"
        return row

    catalog.get = publish_after_read
    with pytest.raises(SnapshotConflict, match="concurrent CAS"):
        await manager._send_event_if_state("snapshot-fork", "fail", {"CREATING"})
    assert catalog.records["snapshot-fork"]["state"] == "LOCAL_READY"
