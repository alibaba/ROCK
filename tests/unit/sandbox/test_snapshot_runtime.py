"""Peer-manager collaboration tests for the single sandbox lifecycle."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from rock.actions.sandbox.response import State
from rock.admin.core.sandbox_table import is_sandbox_identity_conflict
from rock.admin.proto.snapshot import CreateSnapshotRequest, ForkSnapshotRequest, StartFromSnapshotRequest
from rock.admin.service.snapshot_manager import (
    SnapshotConflict,
    SnapshotManager,
    SnapshotNotFound,
    SnapshotResultUnknown,
)
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.manager import DeploymentManager
from rock.sandbox.operator.snapd import (
    OperatorLifecycleError,
    OperatorResultCode,
    OperatorSandboxResult,
    OperatorTarget,
    SnapdOperator,
    SnapdPlacement,
    operator_result_from_info,
    sandbox_info_from_result,
)
from rock.sandbox.sandbox_manager import SandboxManager, _outcome_operator_result
from rock.sandbox.snapshot_types import (
    ForkChildOutcome,
    ForkExecutionResult,
    ForkPreparation,
    LaunchableSnapshot,
)
from rock.sdk.common.exceptions import BadRequestRockError

_ENDPOINT = {
    "transport": "vsock-tcp-forward",
    "host_ip": "10.0.0.1",
    "host_port": 32_100,
    "guest_port": 22_555,
    "vsock_cid": 12_100,
}
_ROCKLET = {
    "identity": "sha256:" + "b" * 64,
    "version": "1.11.0-test",
    "sha256": "sha256:" + "b" * 64,
    "logical_bytes": 64 * 1024 * 1024,
}


def _integrity_error(driver: str, code=None, constraint=None) -> IntegrityError:
    if driver == "sqlite":
        original = sqlite3.IntegrityError("fixture database constraint failure")
        original.sqlite_errorcode = code
    else:
        original = RuntimeError("fixture database constraint failure")
        if driver in {"pgcode", "sqlstate"}:
            setattr(original, driver, code)
            original.diag = SimpleNamespace(constraint_name=constraint)
    return IntegrityError("INSERT", {}, original)


@pytest.mark.parametrize(
    "driver,code,constraint,expected",
    [
        ("pgcode", "23505", "sandbox_record_pkey", True),
        ("sqlstate", "23505", "sandbox_record_pkey", True),
        ("pgcode", "23503", "sandbox_record_pkey", False),
        ("pgcode", "23505", "another_unique_index", False),
        ("sqlstate", "23505", None, False),
        ("sqlite", sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, None, True),
        ("sqlite", sqlite3.SQLITE_CONSTRAINT_NOTNULL, None, False),
        ("sqlite", sqlite3.SQLITE_CONSTRAINT_UNIQUE, None, False),
        ("sqlite", sqlite3.SQLITE_CONSTRAINT, None, False),
        ("unknown", None, None, False),
    ],
)
def test_only_sandbox_primary_key_violations_are_identity_conflicts(driver, code, constraint, expected):
    assert is_sandbox_identity_conflict(_integrity_error(driver, code, constraint)) is expected


def test_operator_observation_round_trip_does_not_serialize_runtime_status() -> None:
    observation = OperatorSandboxResult(
        "vm-a", OperatorResultCode.RESULT_UNKNOWN, "PENDING",
        target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1"),
        last_error="launch acknowledgement lost",
    )
    info = sandbox_info_from_result(observation)
    assert operator_result_from_info(info) is observation
    assert str(OperatorLifecycleError(info)) == "launch acknowledgement lost"
    persisted = json.loads(json.dumps(info))
    assert persisted == {
        "sandbox_id": "vm-a", "state": "pending", "metadata": {},
        "host_ip": "10.0.0.1", "snapd_node_id": "node-a",
    }


class _MetaStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.create_calls = 0

    async def get(self, sandbox_id, *, check_db=False):
        del check_db
        row = self.rows.get(sandbox_id)
        return None if row is None else dict(row)

    async def get_durable(self, sandbox_id):
        return await self.get(sandbox_id)

    async def exists(self, sandbox_id):
        return sandbox_id in self.rows

    async def create(self, sandbox_id, info, *, timeout_info, deployment_config):
        del timeout_info
        self.create_calls += 1
        if sandbox_id in self.rows:
            raise _integrity_error("sqlite", sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY)
        self.rows[sandbox_id] = {**dict(info), "spec": deployment_config.model_dump()}

    async def update(self, sandbox_id, info):
        self.rows[sandbox_id].update(info)

    async def archive(self, sandbox_id, info):
        self.rows[sandbox_id] = dict(info)

    async def list_by(self, field, value):
        return [dict(row) for row in self.rows.values() if row.get(field) == value]

    async def list_snapshot_references(self, snapshot_id):
        return [
            dict(row) for row in self.rows.values()
            if row.get("source_snapshot_id") == snapshot_id or row.get("pause_snapshot_id") == snapshot_id
        ]

    async def get_timeout(self, sandbox_id):
        del sandbox_id
        return None

    async def update_host_ip_if_missing(self, sandbox_id, host_ip):
        row = self.rows[sandbox_id]
        if row.get("host_ip"):
            return False
        row["host_ip"] = host_ip
        return True


class _Operator:
    def __init__(self, store: _MetaStore) -> None:
        self.store = store
        self.submit_calls = []
        self.submit_entered = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.block_submit = False
        self.submit_result = OperatorSandboxResult(
            "fixture",
            OperatorResultCode.RUNNING,
            "RUNNING",

            target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="ray-a"),
            guest_endpoint=_ENDPOINT,
            vmm_pid=4242,
            rocklet_artifact=_ROCKLET,
        )
        self.status_result: OperatorSandboxResult | None = None

    async def submit(self, config, user_info=None):
        sandbox_id = config.container_name
        assert sandbox_id in self.store.rows
        assert self.store.rows[sandbox_id]["state"] in {State.PENDING, State.PENDING.value}
        assert "snapshot_runtime" not in self.store.rows[sandbox_id]
        self.submit_calls.append(config)
        self.submit_entered.set()
        if self.block_submit:
            await self.release_submit.wait()
        result = OperatorSandboxResult(
            sandbox_id,
            self.submit_result.code,
            self.submit_result.state,

            target=self.submit_result.target,
            last_error=self.submit_result.last_error,
            guest_endpoint=self.submit_result.guest_endpoint,
            vmm_pid=self.submit_result.vmm_pid,
            rocklet_artifact=self.submit_result.rocklet_artifact,
            metadata=dict(config.metadata),
        )
        info = sandbox_info_from_result(result)
        if result.result_unknown:
            raise OperatorLifecycleError(info)
        return info

    async def get_status(self, sandbox_id):
        del sandbox_id
        return None if self.status_result is None else sandbox_info_from_result(self.status_result)


class _SnapshotManagerStub:
    def __init__(self, store: _MetaStore) -> None:
        self.store = store
        self.captured = []
        self.execute_calls = 0
        self.raise_unknown = False
        self.raise_conflict = False
        self.fork_result: ForkExecutionResult | None = None
        self.completed = []
        self.launchable_calls: list[str] = []
        self.started = []

    async def require_launchable_snapshot(self, snapshot_id):
        self.launchable_calls.append(snapshot_id)
        return _snapshot(snapshot_id)

    async def get_launchable_snapshot(self, snapshot_id):
        return await self.require_launchable_snapshot(snapshot_id)

    async def start_async(
        self, snapshot_id, request, *, sandbox_namespace, sandbox_image=None,
        deployment_config=None, sandbox_metadata=None,
    ):
        self.started.append((snapshot_id, request, sandbox_namespace, sandbox_image))
        return await self.sandbox_manager.start_from_snapshot(
            await self.require_launchable_snapshot(snapshot_id), request,
            sandbox_namespace=sandbox_namespace, sandbox_image=sandbox_image,
            deployment_config=deployment_config, sandbox_metadata=sandbox_metadata,
        )

    async def capture_snapshot(self, source, request):
        self.captured.append((source, request))
        return {"snapshot_id": request.snapshot_id or "capture-1", "state": "LOCAL_READY"}

    async def prepare_fork(self, source, request, *, child_timeout_seconds):
        targets = tuple(request.target_sandbox_ids or ("child-a", "child-b"))
        return ForkPreparation(
            source=source,
            snapshot_id=request.snapshot_id or "fork-1",
            sandbox_namespace=source.sandbox_namespace,

            target_sandbox_ids=targets,
            resource_spec={**source.resource_spec, **request.resource_spec},
            child_timeout_seconds=child_timeout_seconds,
        )

    async def execute_fork(self, preparation, children):
        self.execute_calls += 1
        assert preparation.target_sandbox_ids == tuple(child.sandbox_id for child in children)
        assert all(child.sandbox_id in self.store.rows for child in children)
        assert all(
            self.store.rows[child.sandbox_id]["state"] in {State.PENDING, State.PENDING.value} for child in children
        )
        if self.raise_unknown:
            return ForkExecutionResult(
                OperatorResultCode.RESULT_UNKNOWN,
                "CREATING",
                tuple(ForkChildOutcome(child.sandbox_id, "PENDING") for child in children),
                last_error="lost fork reply",
            )
        if self.raise_conflict:
            raise SnapshotConflict("definitive fork rejection")
        if self.fork_result is not None:
            return self.fork_result
        return ForkExecutionResult(
            OperatorResultCode.ACCEPTED,
            "LOCAL_READY",
            tuple(
                ForkChildOutcome(
                    child.sandbox_id,
                    "RUNNING",
                    OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="ray-a"),
                    guest_endpoint={**_ENDPOINT, "host_port": 32_100 + index, "vsock_cid": 12_100 + index},
                    vmm_pid=4300 + index,
                    rocklet_artifact=_ROCKLET,
                )
                for index, child in enumerate(children)
            ),
        )

    async def complete_fork(self, preparation, result):
        self.completed.append((preparation, result))
        state = (
            "FAILED"
            if result.code in {OperatorResultCode.FAILED, OperatorResultCode.IDENTITY_CONFLICT}
            else "LOCAL_READY"
        )
        return {"snapshot_id": preparation.snapshot_id, "state": state}

    async def get_snapshot_record(self, snapshot_id):
        return {"snapshot_id": snapshot_id, "state": "LOCAL_READY"}


def _snapshot(snapshot_id="base-1") -> LaunchableSnapshot:
    spec = {"backend_payload": {"value": snapshot_id}}
    return LaunchableSnapshot(snapshot_id, "BASE_IMAGE", 1, spec, "sha256:test", "10.0.0.9")


def _manager() -> tuple[SandboxManager, _MetaStore, _Operator, _SnapshotManagerStub]:
    store = _MetaStore()
    operator = _Operator(store)
    snapshots = _SnapshotManagerStub(store)
    manager = object.__new__(SandboxManager)
    manager._meta_store = store
    manager._operator = operator
    manager._snapshot_manager = None
    manager.rock_config = RockConfig(aes_encrypt_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    manager.rock_config.update = AsyncMock()
    manager.deployment_manager = DeploymentManager(manager.rock_config)
    manager._aes_encrypter = SimpleNamespace(encrypt=lambda value: f"encrypted:{value}")
    manager._snapshot_template_start_timeout_seconds = 60
    manager.attach_snapshot_manager(snapshots)  # type: ignore[arg-type]
    snapshots.sandbox_manager = manager
    return manager, store, operator, snapshots


@pytest.mark.parametrize(
    "state,checkpoint,expected",
    [
        (State.RUNNING, False, True),
        (State.PENDING, False, True),
        ("future-state", False, True),
        (State.STOPPED, False, False),
        (State.STOPPED, True, True),
        (State.ARCHIVED, True, False),
        (State.DELETED, True, False),
    ],
)
async def test_active_snapshot_users_are_projected_from_durable_references(state, checkpoint, expected):
    manager, store, _, _ = _manager()
    store.rows["vm"] = {
        **_source_row(), "sandbox_id": "vm", "state": state,
        "source_snapshot_id": "other" if checkpoint else "base-1",
        "pause_snapshot_id": "base-1" if checkpoint else None,
        "namespace": "business-project", "user_id": "owner-a",
    }
    store.rows["unrelated"] = {"sandbox_id": "unrelated", "state": State.RUNNING}

    users = await manager.list_active_snapshot_users("base-1")

    assert len(users) == int(expected)
    if expected:
        assert asdict(users[0]) == {
            "sandbox_id": "vm", "source_snapshot_id": "base-1",
            "lifecycle_state": str(getattr(state, "value", state)),
            "placement": {"node_id": "node-a", "host_ip": "10.0.0.1", "host_name": "ray-a"},
        }


async def test_active_snapshot_users_do_not_treat_namespace_as_an_authorization_boundary():
    manager, store, _, _ = _manager()
    for sandbox_id, namespace in (("vm-a", "project-a"), ("vm-b", "project-b")):
        store.rows[sandbox_id] = {
            **_source_row(), "sandbox_id": sandbox_id, "namespace": namespace, "host_ip": "default",
        }

    users = await manager.list_active_snapshot_users("base-1")

    assert [user.sandbox_id for user in users] == ["vm-a", "vm-b"]
    assert all(user.placement == {} for user in users)


def _source_row() -> dict:
    return {
        "sandbox_id": "source",
        "namespace": "tenant-a",
        "state": State.RUNNING,
        "image": "snapshot:base-1",
        "source_snapshot_id": "base-1",
        "snapd_node_id": "node-a",
        "host_ip": "10.0.0.1",
        "host_name": "ray-a",
        "cpus": 1,
        "memory": "128M",
    }


def test_manager_port_is_bind_once_and_required() -> None:
    manager = object.__new__(SandboxManager)
    manager._snapshot_manager = None
    with pytest.raises(RuntimeError, match="not attached"):
        manager._require_snapshot_manager()
    port = object()
    manager.attach_snapshot_manager(port)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="already attached"):
        manager.attach_snapshot_manager(port)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_start_from_snapshot_is_durable_before_operator_rpc() -> None:
    manager, store, operator, _ = _manager()

    result = await manager.start_from_snapshot(
        _snapshot(),
        StartFromSnapshotRequest(sandbox_id="vm-a", resource_spec={"vcpus": 1, "memory_mib": 128}),
        sandbox_namespace="tenant-a",
    )

    assert result == {"sandbox_id": "vm-a", "source_snapshot_id": "base-1", "state": "RUNNING"}
    assert store.create_calls == 1
    assert len(operator.submit_calls) == 1
    assert operator.submit_calls[0].source_snapshot_id == "base-1"
    assert store.rows["vm-a"]["source_snapshot_id"] == "base-1"
    assert "snapshot_runtime" not in store.rows["vm-a"]
    assert "runtime_epoch" not in store.rows["vm-a"]
    assert "guest_endpoint" not in store.rows["vm-a"]
    assert "rocklet_artifact" not in store.rows["vm-a"]
    assert "vmm_pid" not in store.rows["vm-a"]
    assert store.rows["vm-a"]["snapd_node_id"] == "node-a"
    assert store.rows["vm-a"]["port_mapping"] == {22_555: 32_100}
    assert str(getattr(store.rows["vm-a"]["state"], "value", store.rows["vm-a"]["state"])) == "running"


async def test_snapd_start_receives_resolved_snapshot_data_without_second_catalog_read() -> None:
    manager, store, delegate, snapshots = _manager()
    operator = object.__new__(SnapdOperator)
    store.get_durable = AsyncMock(wraps=store.get_durable)

    async def submit(config, *, runtime):
        assert store.rows["vm-a"]["state"] == State.PENDING
        store.get_durable.assert_not_awaited()
        assert runtime.snapshot_spec == _snapshot().spec
        return await delegate.submit(config)

    operator.submit = AsyncMock(side_effect=submit)
    manager._operator = operator
    await manager.start_from_snapshot(
        _snapshot(),
        StartFromSnapshotRequest(sandbox_id="vm-a", resource_spec={"vcpus": 2, "memory_mib": 512, "disk": "2G"}),
        sandbox_namespace="tenant-a",
    )

    runtime = operator.submit.await_args.kwargs["runtime"]
    # The boundary contains serializable values only, never a table, metastore or provider.
    assert json.loads(json.dumps(asdict(runtime))) == {
        "sandbox_id": "vm-a",
        "source_snapshot_id": "base-1",
        "sandbox_namespace": "tenant-a",
        "placement": None,
        "resource_spec": {"vcpus": 2, "memory_mib": 512, "disk": "2G"},
        "snapshot_spec": _snapshot().spec,
        "source_worker_ip": "10.0.0.9",
    }
    assert snapshots.launchable_calls == []
    assert "snapshot_spec" not in store.rows["vm-a"]["spec"]
    assert "runtime" not in store.rows["vm-a"]["spec"]


@pytest.mark.parametrize("state,spec", [("CREATING", {}), ("LOCAL_READY", {}), ("REMOTE_READY", None)])
async def test_snapshot_manager_rejects_unlaunchable_snapshot_before_sandbox_admission(state, spec) -> None:
    manager, store, delegate, _ = _manager()
    operator = object.__new__(SnapdOperator)
    operator.submit = AsyncMock()
    manager._operator = operator
    table = AsyncMock()
    table.get.return_value = {"snapshot_id": "base-1", "state": state, "spec": spec}
    snapshots = SnapshotManager(table, operator, manager)

    with pytest.raises(SnapshotConflict, match="not REMOTE_READY"):
        await snapshots.start_async("base-1", StartFromSnapshotRequest(sandbox_id="vm-a"))

    table.get.assert_awaited_once_with("base-1")
    assert store.create_calls == 0 and not store.rows
    assert delegate.submit_calls == []
    operator.submit.assert_not_awaited()


@pytest.mark.parametrize("operation", ["status", "reconcile_pending"])
async def test_manager_supplies_durable_recovery_placement_without_loading_snapshot(operation) -> None:
    manager, store, _, snapshots = _manager()
    row = {
        **_source_row(),
        "state": State.PENDING,
        "disk": "2G",
        "cpus": 2,
        "memory": "512M",
    }
    store.rows["source"] = row
    store.get_durable = AsyncMock(return_value=dict(row))
    store.get = AsyncMock(side_effect=AssertionError("must use the durable sandbox row"))
    operator = object.__new__(SnapdOperator)
    operator.get_status = AsyncMock(return_value=None)
    manager._operator = operator

    if operation == "status":
        await manager.get_status("source", include_all_states=True, refresh_timeout=False)
    else:
        sm = await manager._get_current_statemachine("source")
        await manager._try_advance_pending("source", sm)

    store.get_durable.assert_awaited_once_with("source")
    runtime = operator.get_status.await_args.kwargs["runtime"]
    assert operator.get_status.await_args.args == ("source",)
    assert runtime.sandbox_namespace == "tenant-a"
    assert runtime.placement == SnapdPlacement("node-a", "10.0.0.1", "ray-a")
    assert runtime.resource_spec == {"vcpus": 2, "memory_mib": 512, "disk": "2G"}
    assert runtime.snapshot_spec is None and runtime.source_worker_ip is None
    assert snapshots.launchable_calls == []


def test_manager_rejects_incomplete_durable_snapd_placement() -> None:
    manager, _, _, _ = _manager()
    manager._operator = object.__new__(SnapdOperator)
    row = {**_source_row(), "snapd_node_id": None, "host_name": None}
    with pytest.raises(ValueError, match="placement is incomplete"):
        manager._snapd_runtime(row)


@pytest.mark.asyncio
async def test_snapshot_start_without_sandbox_id_generates_random_unprefixed_uuid_each_time() -> None:
    manager, store, operator, _ = _manager()
    request = StartFromSnapshotRequest(resource_spec={})

    first = await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")
    second = await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")

    assert first["sandbox_id"] != second["sandbox_id"]
    for result in (first, second):
        generated = result["sandbox_id"]
        assert uuid.UUID(hex=generated).version == 4
        assert uuid.UUID(hex=generated).hex == generated
        assert len(generated) == 32
        assert generated in store.rows
    assert len(operator.submit_calls) == 2
    assert request.sandbox_id is None


@pytest.mark.asyncio
async def test_snapshot_start_preserves_explicit_id_and_rejects_duplicate_without_rpc() -> None:
    manager, store, operator, _ = _manager()
    request = StartFromSnapshotRequest(sandbox_id="user-chosen-sandbox", resource_spec={})

    first = await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")
    original = dict(store.rows["user-chosen-sandbox"])
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")

    assert first["sandbox_id"] == "user-chosen-sandbox"
    assert list(store.rows) == ["user-chosen-sandbox"]
    assert store.rows["user-chosen-sandbox"] == original
    assert len(operator.submit_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_restore_conflicts_while_original_rpc_is_in_flight() -> None:
    manager, store, operator, _ = _manager()
    operator.block_submit = True
    request = StartFromSnapshotRequest(sandbox_id="in-flight-child")
    first = asyncio.create_task(manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a"))
    try:
        await asyncio.wait_for(operator.submit_entered.wait(), timeout=1)
        original = dict(store.rows["in-flight-child"])

        with pytest.raises(SnapshotConflict, match="already exists"):
            await asyncio.wait_for(
                manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a"), timeout=1,
            )

        assert store.rows["in-flight-child"] == original
        assert len(operator.submit_calls) == 1
    finally:
        operator.release_submit.set()
        result = await first
    assert result["sandbox_id"] == "in-flight-child"
    assert result["state"] == "RUNNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("container_name", [None, "user-chosen-template-sandbox"])
async def test_snapshot_template_preserves_explicit_id_or_generates_unprefixed_uuid(container_name) -> None:
    manager, _, _, snapshots = _manager()
    config = DockerDeploymentConfig(image="base-1", container_name=container_name)

    first = await manager.start_async(config, user_info={"namespace": "tenant-a"})

    if container_name is None:
        second = await manager.start_async(config, user_info={"namespace": "tenant-a"})
        assert first.sandbox_id != second.sandbox_id
        for response in (first, second):
            generated = response.sandbox_id
            assert uuid.UUID(hex=generated).version == 4
            assert uuid.UUID(hex=generated).hex == generated
            assert len(generated) == 32
        assert [call[1].sandbox_id for call in snapshots.started] == [first.sandbox_id, second.sandbox_id]
    else:
        assert first.sandbox_id == container_name
        with pytest.raises(BadRequestRockError, match="already exists"):
            await manager.start_async(config, user_info={"namespace": "tenant-a"})
        assert len(snapshots.started) == 1
    assert all(call[2] == "tenant-a" for call in snapshots.started)
    assert config.container_name == container_name


@pytest.mark.parametrize(
    "image,source_snapshot_id",
    [
        ("base-1", None),
        ("registry.example/base:latest", "base-1"),
    ],
)
async def test_native_snapshot_start_returns_resolved_source_identity(image, source_snapshot_id):
    manager, _, _, snapshots = _manager()
    manager._check_sandbox_exists_in_redis = AsyncMock()
    snapshots.get_launchable_snapshot = AsyncMock(return_value=_snapshot())
    config = DockerDeploymentConfig(image=image, source_snapshot_id=source_snapshot_id, container_name="vm-a")

    response = await manager.start_async(config, user_info={"namespace": "tenant-a"})

    assert response.sandbox_id == "vm-a"
    assert response.source_snapshot_id == "base-1"
    assert "template_id" not in response.model_dump()
    assert snapshots.started[0][0] == response.source_snapshot_id
    assert snapshots.started[0][2] == "tenant-a"


@pytest.mark.parametrize("image", ["python:3.11", "registry.example/base:latest"])
async def test_native_explicit_missing_snapshot_does_not_fall_back_to_oci(image):
    manager, store, operator, snapshots = _manager()
    snapshots.get_launchable_snapshot = AsyncMock(return_value=None)
    snapshots.prepare_base_image = AsyncMock(return_value=_snapshot())
    config = DockerDeploymentConfig(
        image=image, source_snapshot_id="missing-snapshot", container_name="vm-missing-snapshot",
    )

    with pytest.raises(SnapshotNotFound, match="missing-snapshot"):
        await manager.start_async(config)

    snapshots.get_launchable_snapshot.assert_awaited_once_with("missing-snapshot")
    snapshots.prepare_base_image.assert_not_awaited()
    assert snapshots.started == []
    assert store.create_calls == 0
    assert operator.submit_calls == []


@pytest.mark.parametrize("snapshot_backend", [False, True])
async def test_native_oci_start_only_returns_snapshot_identity_for_snapshot_backends(snapshot_backend):
    manager, _, _, snapshots = _manager()
    config = DockerDeploymentConfig(image="registry.example/base:latest", container_name="vm-a")
    snapshot = _snapshot()
    snapshots.get_launchable_snapshot = AsyncMock(return_value=None)
    snapshots.prepare_base_image = AsyncMock(return_value=snapshot)
    snapshots.start_async = AsyncMock(return_value={
        "sandbox_id": "vm-a", "source_snapshot_id": "base-1", "state": "RUNNING",
    })
    manager._snapshot_manager = snapshots if snapshot_backend else None

    response = await manager.start_async(config)

    assert response.sandbox_id == "vm-a"
    assert response.source_snapshot_id == ("base-1" if snapshot_backend else None)
    assert "template_id" not in response.model_dump()
    if snapshot_backend:
        assert snapshots.prepare_base_image.await_args.args[0].oci_ref == config.image
        assert snapshots.start_async.await_args.args[0] == response.source_snapshot_id
    else:
        snapshots.prepare_base_image.assert_not_awaited()
        snapshots.start_async.assert_not_awaited()


@pytest.mark.parametrize("start_kind", ["template", "snapshot", "snapd-oci", "ray-oci"])
async def test_common_start_preserves_product_metadata_and_owner(start_kind):
    manager, store, operator, snapshots = _manager()
    config = DockerDeploymentConfig(
        image="template-alias" if start_kind == "template" else "registry.example/base:latest",
        container_name="vm-metadata", cpus=3, memory="256M", disk="2G",
        metadata={"purpose": "evaluation"}, env_vars={"EXPERIMENT_MODE": "test"},
        source_snapshot_id="base-1" if start_kind == "snapshot" else None,
    )
    snapshot = _snapshot()
    snapshots.get_launchable_snapshot = AsyncMock(return_value=snapshot)
    if start_kind == "snapd-oci":
        snapshots.get_launchable_snapshot.return_value = None
        snapshots.prepare_base_image = AsyncMock(return_value=snapshot)
    elif start_kind == "ray-oci":
        manager._snapshot_manager = None

    await manager.start_async(
        config,
        user_info={
            "user_id": "owner-42", "experiment_id": "experiment-7", "namespace": "business-project",
            "rock_authorization": "fixture-authorization",
        },
        cluster_info={"cluster_name": "cluster-a"},
    )

    row = store.rows["vm-metadata"]
    assert row["user_id"] == "owner-42"
    assert row["experiment_id"] == "experiment-7"
    assert row["namespace"] == "business-project"
    assert row["cluster_name"] == "cluster-a"
    assert row["metadata"] == config.metadata
    assert row["env"] == config.env_vars
    assert row["rock_authorization_encrypted"] == "encrypted:fixture-authorization"
    assert row["spec"]["metadata"] == config.metadata
    assert row["spec"]["env_vars"] == config.env_vars
    assert operator.submit_calls[0].cpus == 3
    assert row["create_time"]
    manager.rock_config.update.assert_awaited_once()


@pytest.mark.parametrize("start_kind", ["template", "snapshot", "ray-oci", "dedicated", "fork"])
@pytest.mark.parametrize("resources,resource_spec,message", [
    ({"cpus": 5, "memory": "512M"}, {"vcpus": 5, "memory_mib": 512}, "Requested CPUs"),
    ({"cpus": 1, "memory": "3G"}, {"vcpus": 1, "memory_mib": 3072}, "Requested memory"),
])
async def test_snapshot_starts_and_forks_respect_existing_resource_caps(
    start_kind, resources, resource_spec, message,
):
    manager, store, operator, snapshots = _manager()
    manager.rock_config.runtime.max_allowed_spec.cpus = 4
    manager.rock_config.runtime.max_allowed_spec.memory = "2G"
    if start_kind == "ray-oci":
        manager._snapshot_manager = None
    with pytest.raises(BadRequestRockError, match=message):
        if start_kind == "dedicated":
            await manager.start_from_snapshot(
                _snapshot(), StartFromSnapshotRequest(sandbox_id="new", resource_spec=resource_spec),
                sandbox_namespace="business-project",
            )
        elif start_kind == "fork":
            store.rows["source"] = _source_row()
            await manager.fork_sandbox("source", ForkSnapshotRequest(count=1, resource_spec=resource_spec))
        else:
            await manager.start_async(DockerDeploymentConfig(
                image="base-1", container_name="new", **resources,
                source_snapshot_id="base-1" if start_kind == "snapshot" else None,
            ))

    assert store.create_calls == 0
    assert operator.submit_calls == []
    assert snapshots.started == []
    assert snapshots.execute_calls == 0


@pytest.mark.parametrize("snapshot_backend", [False, True])
@pytest.mark.parametrize("policy", [{}, {"auto_delete_seconds": 900}, {"auto_archive_seconds": 900}])
async def test_snapshot_common_start_applies_the_same_cleanup_policy_as_oci(snapshot_backend, policy):
    manager, store, operator, _ = _manager()
    manager.rock_config.lifecycle.auto_transition.auto_delete_seconds = 120
    if not snapshot_backend:
        manager._snapshot_manager = None

    await manager.start_async(DockerDeploymentConfig(image="base-1", container_name="vm-policy", **policy))

    row = store.rows["vm-policy"]
    config = operator.submit_calls[0]
    policy_field = "auto_archive_seconds" if "auto_archive_seconds" in policy else "auto_delete_seconds"
    assert row[policy_field] == getattr(config, policy_field) == 120
    assert config.remove_container is False
    if policy_field == "auto_archive_seconds":
        assert row.get("auto_delete_seconds") is None


@pytest.mark.parametrize("operation", ["dedicated", "fork"])
async def test_dedicated_snapshot_admission_inherits_cluster_cleanup_policy(operation):
    manager, store, operator, _ = _manager()
    manager.rock_config.lifecycle.auto_transition.auto_delete_seconds = 120
    if operation == "dedicated":
        await manager.start_from_snapshot(
            _snapshot(), StartFromSnapshotRequest(sandbox_id="child-a"), sandbox_namespace="business-project",
        )
        assert operator.submit_calls[0].auto_delete_seconds == 120
    else:
        store.rows["source"] = _source_row()
        await manager.fork_sandbox("source", ForkSnapshotRequest(count=1, target_sandbox_ids=["child-a"]))

    row = store.rows["child-a"]
    assert row["auto_delete_seconds"] == row["spec"]["auto_delete_seconds"] == 120
    assert row["spec"]["remove_container"] is False


async def test_full_snapshot_restores_frozen_geometry_instead_of_default_or_standard_spec():
    manager, store, operator, snapshots = _manager()
    frozen = {"vcpus": 3, "memory_mib": 768, "disk": "4G"}
    full = replace(_snapshot("full-1"), kind="FULL", resource_spec=frozen)
    snapshots.get_launchable_snapshot = AsyncMock(return_value=full)
    snapshots.require_launchable_snapshot = AsyncMock(return_value=full)
    manager.rock_config.runtime.use_standard_spec_only = True
    manager.rock_config.runtime.standard_spec.cpus = 8
    manager.rock_config.runtime.standard_spec.memory = "16G"
    config = DockerDeploymentConfig(image="full-1", container_name="restored")

    response = await manager.start_async(config)

    restored = operator.submit_calls[0]
    assert restored.cpus == response.cpus == frozen["vcpus"]
    assert restored.memory == response.memory == "768M"
    assert restored.disk == response.disk == "4G"
    assert snapshots.started[0][1].resource_spec == frozen
    assert store.rows["restored"]["spec"]["cpus"] == 3
    assert store.rows["restored"]["spec"]["memory"] == "768M"
    assert config.cpus == 2 and config.memory == "8g"


@pytest.mark.parametrize("resources,message", [
    ({"cpus": 2}, "CPU count cannot be changed"),
    ({"memory": "8G"}, "memory size cannot be changed"),
])
async def test_full_snapshot_rejects_explicit_geometry_changes_before_admission(resources, message):
    manager, store, operator, snapshots = _manager()
    full = replace(_snapshot("full-1"), kind="FULL", resource_spec={"vcpus": 3, "memory_mib": 768})
    snapshots.get_launchable_snapshot = AsyncMock(return_value=full)

    with pytest.raises(BadRequestRockError, match=message):
        await manager.start_async(DockerDeploymentConfig(image="full-1", container_name="restored", **resources))

    assert store.create_calls == 0
    assert operator.submit_calls == []
    assert snapshots.started == []


@pytest.mark.asyncio
async def test_shared_snapshot_start_persists_source_snapshot_identity() -> None:
    manager, store, _, _ = _manager()
    request = StartFromSnapshotRequest(
        sandbox_id="vm-template-alias",
        resource_spec={"vcpus": 2, "memory_mib": 256},
    )

    await manager.start_from_snapshot(
        _snapshot("shared-canonical-base"),
        request,
        sandbox_namespace="tenant-a",
        sandbox_image="template-alias",
    )

    assert store.rows["vm-template-alias"]["source_snapshot_id"] == "shared-canonical-base"
    assert store.rows["vm-template-alias"]["image"] == "template-alias"
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.start_from_snapshot(
            _snapshot("shared-canonical-base"),
            request,
            sandbox_namespace="tenant-a",
            sandbox_image="another-template",
        )


@pytest.mark.asyncio
async def test_result_unknown_preserves_pending_row_and_duplicate_conflicts_without_rpc() -> None:
    manager, store, operator, _ = _manager()
    operator.submit_result = OperatorSandboxResult(
        "fixture",
        OperatorResultCode.RESULT_UNKNOWN,
        "PENDING",

        target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1"),
        last_error="reply lost",
    )
    request = StartFromSnapshotRequest(sandbox_id="vm-a", resource_spec={})

    with pytest.raises(SnapshotResultUnknown, match="reply lost"):
        await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.start_from_snapshot(_snapshot(), request, sandbox_namespace="tenant-a")

    assert len(store.rows) == 1
    assert store.create_calls == 2
    assert len(operator.submit_calls) == 1
    assert str(getattr(store.rows["vm-a"]["state"], "value", store.rows["vm-a"]["state"])) == "pending"
    assert "snapshot_runtime" not in store.rows["vm-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_spec", [{"vcpus": 1, "memory_mib": 512}, {"vcpus": 2}])
async def test_existing_restore_identity_conflicts_regardless_of_resources(resource_spec) -> None:
    manager, store, operator, _ = _manager()
    await manager.start_from_snapshot(
        _snapshot(), StartFromSnapshotRequest(sandbox_id="vm-a", resource_spec={}),
        sandbox_namespace="tenant-a",
    )
    original = dict(store.rows["vm-a"])
    with pytest.raises(SnapshotConflict, match="already exists"):
        await manager.start_from_snapshot(
            _snapshot(), StartFromSnapshotRequest(sandbox_id="vm-a", resource_spec=resource_spec),
            sandbox_namespace="tenant-a",
        )
    assert store.rows["vm-a"] == original
    assert len(operator.submit_calls) == 1


@pytest.mark.asyncio
async def test_status_uses_rootfs_size_summary_without_reading_snapshot_spec() -> None:
    manager, store, operator, snapshots = _manager()
    store.rows["vm-a"] = {**_source_row(), "sandbox_id": "vm-a"}
    operator.status_result = OperatorSandboxResult(
        "vm-a", OperatorResultCode.RUNNING, "RUNNING", target=OperatorTarget(host_ip="10.0.0.1"),
        guest_endpoint=_ENDPOINT, vmm_pid=4242, rocklet_artifact=_ROCKLET,
    )
    snapshots.require_launchable_snapshot = AsyncMock(return_value=replace(
        _snapshot(), spec={"backend_v2": [None, "opaque"]}, resource_spec={"rootfs_bytes": 67108864},
    ))
    result = await manager.get_status("vm-a", include_all_states=True)
    assert result.disk == result.disk_limit_rootfs == "67108864B"


@pytest.mark.asyncio
async def test_status_reconcile_uses_standard_state_machine_for_terminal_runtime() -> None:
    manager, store, operator, snapshots = _manager()
    store.rows["vm-a"] = {
        **_source_row(),
        "sandbox_id": "vm-a",
        "state": State.RUNNING,
        "source_snapshot_id": "base-1",
    }
    operator.status_result = OperatorSandboxResult(
        "vm-a",
        OperatorResultCode.FAILED,
        "FAILED",

        target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="ray-a"),
        last_error="runtime exited",
    )

    result = await manager.get_status("vm-a", include_all_states=True)

    assert result.state == State.STOPPED
    assert store.rows["vm-a"]["state"] == State.STOPPED
    assert store.rows["vm-a"]["last_error"] == "runtime exited"
    assert store.rows["vm-a"]["stop_time"]
    assert snapshots.launchable_calls == []


@pytest.mark.asyncio
async def test_capture_route_root_passes_validated_source_projection() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()

    record = await manager.snapshot_sandbox(
        "source",
        CreateSnapshotRequest(snapshot_id="capture-1"),
    )

    source, _ = snapshots.captured[0]
    assert record["state"] == "LOCAL_READY"
    assert source.sandbox_id == "source"
  # Authoritative snapd resolves the live incarnation.
    assert source.target.host_ip == "10.0.0.1"


@pytest.mark.asyncio
async def test_fork_inserts_children_individually_before_external_rpc() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    request = ForkSnapshotRequest(
        snapshot_id="fork-1",
        count=2,
        target_sandbox_ids=["child-a", "child-b"],
        resource_spec={"vcpus": 1, "memory_mib": 128},
    )

    record, children = await manager.fork_sandbox(
        "source",
        request,
        child_timeout_seconds=37,
    )

    assert store.create_calls == 2
    assert snapshots.execute_calls == 1
    assert record["state"] == "LOCAL_READY"
    assert [child["sandbox_id"] for child in children] == ["child-a", "child-b"]
    assert all(child["state"] == "RUNNING" for child in children)
    assert [child["guest_endpoint"]["host_port"] for child in children] == [32_100, 32_101]
    assert store.rows["child-a"]["port_mapping"] == {22_555: 32_100}
    assert store.rows["child-b"]["port_mapping"] == {22_555: 32_101}
    assert all("fork_identity" not in store.rows[child] for child in ("child-a", "child-b"))


@pytest.mark.asyncio
async def test_fork_unknown_keeps_snapshot_and_children_pending_for_status_reconciliation() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    snapshots.raise_unknown = True
    request = ForkSnapshotRequest(
        snapshot_id="fork-1",
        count=2,
        target_sandbox_ids=["child-a", "child-b"],
    )

    with pytest.raises(SnapshotResultUnknown, match="lost fork reply"):
        await manager.fork_sandbox("source", request)

    assert snapshots.execute_calls == 1
    assert snapshots.completed == []
    assert all(
        str(getattr(store.rows[child]["state"], "value", store.rows[child]["state"])) == "pending"
        for child in ("child-a", "child-b")
    )


@pytest.mark.asyncio
async def test_shared_fork_unknown_persists_failed_admission_and_surviving_pending_placement() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    manager._mark_launch_failed = AsyncMock(wraps=manager._mark_launch_failed)
    target = OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="ray-a")
    snapshots.fork_result = ForkExecutionResult(
        OperatorResultCode.RESULT_UNKNOWN,
        "CREATING",
        (
            ForkChildOutcome("child-a", "FAILED", last_error="actor reservation failed"),
            ForkChildOutcome("child-b", "PENDING", target, last_error="lost shared fork reply"),
        ),
        last_error="lost shared fork reply",
    )

    with pytest.raises(SnapshotResultUnknown, match="lost shared fork reply"):
        await manager.fork_sandbox("source", ForkSnapshotRequest(count=2))

    assert snapshots.completed == []
    assert store.rows["child-a"]["state"] is State.STOPPED
    assert store.rows["child-a"]["last_error"] == "actor reservation failed"
    assert store.rows["child-b"]["state"] is State.PENDING
    assert store.rows["child-b"]["host_ip"] == target.host_ip
    assert store.rows["child-b"]["host_name"] == target.host_name
    assert store.rows["child-b"]["snapd_node_id"] == target.node_id
    assert not store.rows["child-b"].get("port_mapping")
    manager._mark_launch_failed.assert_awaited_once_with("child-a", "actor reservation failed")


@pytest.mark.asyncio
async def test_definitive_fork_failure_transitions_snapshot_and_every_reserved_child() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    snapshots.raise_conflict = True

    with pytest.raises(SnapshotConflict, match="definitive fork rejection"):
        await manager.fork_sandbox(
            "source",
            ForkSnapshotRequest(
                snapshot_id="fork-1",
                count=2,
                target_sandbox_ids=["child-a", "child-b"],
            ),
        )

    assert snapshots.execute_calls == 1
    assert all(
        str(getattr(store.rows[child]["state"], "value", store.rows[child]["state"])) == "stopped"
        for child in ("child-a", "child-b")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_children", [("child-a",), ("child-b",), ("child-a", "child-b")])
async def test_fork_skips_conflicting_children_without_mutating_them(existing_children) -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    for child in existing_children:
        store.rows[child] = {"sandbox_id": child, "namespace": "tenant-b", "state": State.RUNNING}
    original = {key: dict(row) for key, row in store.rows.items()}

    record, children = await manager.fork_sandbox(
        "source",
        ForkSnapshotRequest(
            snapshot_id="fork-1",
            count=2,
            target_sandbox_ids=["child-a", "child-b"],
        ),
    )

    accepted = tuple(child for child in ("child-a", "child-b") if child not in existing_children)
    assert store.create_calls == 2
    assert snapshots.execute_calls == bool(accepted)
    assert [child["sandbox_id"] for child in children] == list(accepted)
    assert all(child["state"] == "RUNNING" for child in children)
    assert all(store.rows[key] == row for key, row in original.items())
    assert snapshots.completed[0][0].target_sandbox_ids == accepted
    assert record["state"] == ("LOCAL_READY" if accepted else "FAILED")
    if not accepted:
        assert snapshots.completed[0][1].code is OperatorResultCode.IDENTITY_CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "fork"])
@pytest.mark.parametrize("identity_exists", [False, True])
async def test_non_collision_insert_error_is_not_suppressed(operation, identity_exists, monkeypatch) -> None:
    manager, store, operator, snapshots = _manager()
    store.rows["source"] = _source_row()
    if identity_exists:
        store.rows["new"] = {"sandbox_id": "new", "namespace": "another-tenant", "state": State.RUNNING}
    original_rows = {identity: dict(row) for identity, row in store.rows.items()}
    error = _integrity_error("sqlite", sqlite3.SQLITE_CONSTRAINT_NOTNULL)

    async def fail_create(*args, **kwargs):
        raise error

    monkeypatch.setattr(store, "create", fail_create)
    with pytest.raises(IntegrityError) as caught:
        if operation == "start":
            await manager.start_from_snapshot(
                _snapshot(), StartFromSnapshotRequest(sandbox_id="new"), sandbox_namespace="tenant-a",
            )
        else:
            await manager.fork_sandbox("source", ForkSnapshotRequest(count=1, target_sandbox_ids=["new"]))
    assert caught.value is error
    assert operator.submit_calls == []
    assert snapshots.execute_calls == 0
    assert store.rows == original_rows


@pytest.mark.asyncio
async def test_partial_admission_unknown_fork_preserves_only_new_pending_child() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    store.rows["child-a"] = {"sandbox_id": "child-a", "namespace": "tenant-b", "state": State.RUNNING}
    original = dict(store.rows["child-a"])
    snapshots.raise_unknown = True

    with pytest.raises(SnapshotResultUnknown):
        await manager.fork_sandbox("source", ForkSnapshotRequest(count=2))

    assert snapshots.execute_calls == 1
    assert snapshots.completed == []
    assert store.rows["child-a"] == original
    assert store.rows["child-b"]["state"] == State.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [OperatorResultCode.IDENTITY_CONFLICT, OperatorResultCode.FAILED])
async def test_snapd_fork_rejection_is_persisted_then_propagated(code) -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    original_source = dict(store.rows["source"])
    snapshots.fork_result = ForkExecutionResult(
        code,
        "FAILED",
        tuple(ForkChildOutcome(child, "FAILED", last_error="source cut already in flight") for child in ("child-a", "child-b")),
        last_error="source cut already in flight",
    )

    with pytest.raises(SnapshotConflict, match="source cut already in flight"):
        await manager.fork_sandbox("source", ForkSnapshotRequest(count=2))

    assert snapshots.execute_calls == 1
    assert snapshots.completed[0][1].code is code
    assert store.rows["source"] == original_source
    for child in ("child-a", "child-b"):
        assert store.rows[child]["state"] is State.STOPPED
        assert store.rows[child]["last_error"] == "source cut already in flight"


@pytest.mark.asyncio
@pytest.mark.parametrize("other_state", ["FAILED", "PENDING"])
async def test_partial_child_failure_still_returns_independent_children(other_state) -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    manager._mark_launch_failed = AsyncMock(wraps=manager._mark_launch_failed)
    target = OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="ray-a")
    snapshots.fork_result = ForkExecutionResult(
        OperatorResultCode.ACCEPTED,
        "LOCAL_READY",
        (
            ForkChildOutcome(
                "child-a", "RUNNING", target,
                guest_endpoint=_ENDPOINT, vmm_pid=4242, rocklet_artifact=_ROCKLET,
            ),
            ForkChildOutcome("child-b", other_state, target, last_error="child launch observation"),
        ),
    )

    record, children = await manager.fork_sandbox("source", ForkSnapshotRequest(count=2))

    assert record["state"] == "LOCAL_READY"
    assert [child["state"] for child in children] == ["RUNNING", other_state]
    assert children[1]["last_error"] == "child launch observation"
    assert "guest_endpoint" not in children[1]
    assert store.rows["child-a"]["state"] is State.RUNNING
    assert store.rows["child-a"]["port_mapping"] == {22_555: _ENDPOINT["host_port"]}
    if other_state == "PENDING":
        manager._mark_launch_failed.assert_not_awaited()
        assert store.rows["child-b"]["state"] is State.PENDING
        assert _outcome_operator_result(snapshots.fork_result.children[1]).result_unknown
    else:
        manager._mark_launch_failed.assert_awaited_once_with("child-b", "child launch observation")
        assert store.rows["child-b"]["state"] is State.STOPPED


@pytest.mark.asyncio
async def test_no_admitted_runtime_keeps_unknown_child_pending_when_cut_is_not_submitted() -> None:
    manager, store, _, snapshots = _manager()
    store.rows["source"] = _source_row()
    manager._mark_launch_failed = AsyncMock(wraps=manager._mark_launch_failed)
    snapshots.fork_result = ForkExecutionResult(
        OperatorResultCode.FAILED,
        "FAILED",
        (
            ForkChildOutcome("child-a", "PENDING", last_error="actor cleanup is unknown"),
            ForkChildOutcome("child-b", "FAILED", last_error="actor reservation rejected"),
        ),
        last_error="no child could be admitted; snapshot was not submitted",
    )

    with pytest.raises(SnapshotConflict, match="snapshot was not submitted"):
        await manager.fork_sandbox("source", ForkSnapshotRequest(count=2))

    assert store.rows["child-a"]["state"] is State.PENDING
    assert store.rows["child-b"]["state"] is State.STOPPED
    manager._mark_launch_failed.assert_awaited_once_with("child-b", "actor reservation rejected")
    assert snapshots.completed[0][1].code is OperatorResultCode.FAILED
