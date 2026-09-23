import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import env_vars
from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStartResponse
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotNotFound, SnapshotResultUnknown
from rock.admin.snapd.client import SnapdResultUnknown
from rock.common.constants import StopReason
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.snapd import (
    OperatorLifecycleError,
    OperatorResultCode,
    SnapdOperator,
    SnapdPlacement,
    sandbox_info_from_result,
)
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.snapshot_types import LaunchableSnapshot
from rock.sdk.common.exceptions import BadRequestRockError
from tests.unit.sandbox.test_snapshot_runtime import _ENDPOINT, _MetaStore, _Operator


class _CheckpointStore(_MetaStore):
    def __init__(self):
        super().__init__()
        self.timeout = None

    async def get_timeout(self, sandbox_id):
        return None if self.timeout is None else dict(self.timeout)

    async def update_timeout(self, sandbox_id, timeout_info):
        self.timeout = dict(timeout_info)

    async def archive(self, sandbox_id, info):
        await super().archive(sandbox_id, info)
        self.timeout = None


@pytest.fixture
def manager():
    store = _CheckpointStore()
    config = DockerDeploymentConfig(container_name="vm", source_snapshot_id="base", remove_container=False)
    store.rows["vm"] = dict(
        sandbox_id="vm",
        state=State.RUNNING,
        namespace="default",
        image="template",
        source_snapshot_id="base",
        spec=config.model_dump(),
        host_ip="10.0.0.1",
        host_name="ray-a",
        snapd_node_id="node-a",
        metadata={},
        auth_token="preserved-token",
        port_mapping={22555: _ENDPOINT["host_port"]},
    )
    store.persist_lifecycle = AsyncMock(wraps=store.update)
    store.update_timeout = AsyncMock(wraps=store.update_timeout)
    operator = _Operator(store)
    result = replace(operator.submit_result, sandbox_id="vm")
    operator.submit = AsyncMock()
    operator.restart = AsyncMock()
    operator.get_status = AsyncMock(return_value=sandbox_info_from_result(result))
    operator.stop = AsyncMock()
    operator.delete = AsyncMock()
    operator.archive = AsyncMock()
    operator.supports_running_delete = True
    cfg = RockConfig(aes_encrypt_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    with (
        patch("rock.sandbox.base_manager.BaseManager._setup_scheduler"),
        patch("rock.sandbox.base_manager.MetricsMonitor.create", return_value=None),
    ):
        manager = SandboxManager(
            rock_config=cfg,
            meta_store=store,
            ray_namespace="test",
            ray_service=MagicMock(),
            enable_runtime_auto_clear=False,
            operator=operator,
        )
    manager._snapshot_manager = AsyncMock()
    manager._snapshot_manager.require_launchable_snapshot.return_value = None
    manager._snapshot_manager.get_launchable_snapshot = manager._snapshot_manager.require_launchable_snapshot
    manager._snapshot_manager.get_snapshot_record.side_effect = SnapshotNotFound("not created")
    return manager


@pytest.fixture
def snapd_manager(manager):
    previous = manager._operator
    operator = object.__new__(SnapdOperator)
    for name in ("submit", "restart", "get_status", "stop", "delete"):
        setattr(operator, name, getattr(previous, name))
    manager._operator = operator
    manager._meta_store.rows["vm"].update(namespace="tenant-a", cpus=2, memory="512M", disk="2G")
    manager._snapshot_manager.require_launchable_snapshot.side_effect = lambda snapshot_id: LaunchableSnapshot(
        snapshot_id, "FULL", 1, {"backend_payload": {"value": "opaque"}}, "sha256:test", "10.0.0.9",
        resource_spec={"vcpus": 2, "memory_mib": 512, "disk": "2G"},
    )
    return manager


async def test_snapd_checkpoint_stop_restart_forwards_manager_prepared_runtime(snapd_manager):
    manager = snapd_manager
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    stop_runtime = manager._operator.stop.await_args.kwargs["runtime"]
    assert stop_runtime.source_snapshot_id == "base"
    assert stop_runtime.snapshot_spec is None
    assert stop_runtime.placement == SnapdPlacement("node-a", "10.0.0.1", "ray-a")
    assert stop_runtime.sandbox_namespace == "tenant-a"
    assert stop_runtime.resource_spec == {"vcpus": 2, "memory_mib": 512, "disk": "2G"}

    async def restart(config, *, host_ip, runtime):
        assert manager._meta_store.rows["vm"]["state"] == State.PENDING
        assert config.source_snapshot_id == runtime.source_snapshot_id == checkpoint
        assert host_ip == runtime.placement.host_ip == "10.0.0.1"
        assert runtime.sandbox_namespace == "tenant-a"
        assert runtime.resource_spec == stop_runtime.resource_spec
        assert runtime.snapshot_spec == {"backend_payload": {"value": "opaque"}}
        assert runtime.source_worker_ip == "10.0.0.9"

    manager._operator.restart.side_effect = restart
    await manager.restart_async("vm")
    manager._snapshot_manager.require_launchable_snapshot.assert_awaited_once_with(checkpoint)
    manager._operator.restart.assert_awaited_once()
    manager._operator.submit.assert_not_awaited()
    assert manager._meta_store.rows["vm"]["state"] == State.RUNNING


async def test_snapd_restart_rejects_unready_checkpoint_before_state_change_or_actor_call(snapd_manager):
    manager = snapd_manager
    await manager.stop("vm")
    manager._snapshot_manager.require_launchable_snapshot.side_effect = SnapshotConflict("not REMOTE_READY")
    with pytest.raises(SnapshotConflict, match="not REMOTE_READY"):
        await manager.restart_async("vm")
    manager._operator.restart.assert_not_awaited()
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.STOPPED
    assert row["source_snapshot_id"] == "base" and row["pause_snapshot_id"]


async def test_snapd_expired_stop_forwards_recovery_runtime_without_checkpoint(snapd_manager):
    manager = snapd_manager
    await manager.stop("vm", reason=StopReason.EXPIRED)
    call = manager._operator.stop.await_args
    assert call.args == ("vm",) and call.kwargs["reason"] is StopReason.EXPIRED
    assert call.kwargs["runtime"].placement == SnapdPlacement("node-a", "10.0.0.1", "ray-a")
    assert call.kwargs["runtime"].snapshot_spec is None
    manager._snapshot_manager.capture_snapshot.assert_not_awaited()
    assert manager._meta_store.rows["vm"]["state"] == State.STOPPED


@pytest.mark.parametrize("state", [State.RUNNING, State.STOPPED])
async def test_snapd_delete_forwards_recovery_runtime_in_both_fsm_paths(snapd_manager, state):
    manager = snapd_manager
    manager._meta_store.rows["vm"]["state"] = state
    await manager.delete("vm")
    call = manager._operator.delete.await_args
    assert call.args[0].container_name == "vm"
    assert call.kwargs["host_ip"] == "10.0.0.1"
    runtime = call.kwargs["runtime"]
    assert runtime.sandbox_id == "vm" and runtime.sandbox_namespace == "tenant-a"
    assert runtime.placement == SnapdPlacement("node-a", "10.0.0.1", "ray-a")
    assert runtime.resource_spec == {"vcpus": 2, "memory_mib": 512, "disk": "2G"}
    assert runtime.snapshot_spec is None
    assert manager._meta_store.rows["vm"]["state"] == State.DELETED


async def test_checkpoint_stop_restart_use_existing_states_and_preserve_identity(manager):
    assert await manager.stop("vm") is True
    row = manager._meta_store.rows["vm"]
    checkpoint = row["pause_snapshot_id"]
    assert row["state"] == State.STOPPED
    assert 22555 not in row["port_mapping"]
    assert row["stop_time"] and row["auto_transition_time"] is None

    async def assert_admitted_before_restart(config, *, host_ip):
        durable = manager._meta_store.rows["vm"]
        assert durable["state"] == State.PENDING
        assert durable["pause_snapshot_id"] == checkpoint
        assert durable["source_snapshot_id"] == durable["spec"]["source_snapshot_id"] == checkpoint
        assert config.container_name == "vm" and config.source_snapshot_id == checkpoint
        assert host_ip == "10.0.0.1"
        assert manager._meta_store.timeout[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "10"

    manager._operator.restart.side_effect = assert_admitted_before_restart
    response = await manager.restart_async("vm", timeout_seconds=600)
    assert isinstance(response, SandboxStartResponse) and response.sandbox_id == "vm"
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.RUNNING
    assert row["auth_token"] == "preserved-token" and row["namespace"] == "default"
    assert row["source_snapshot_id"] == checkpoint and row["pause_snapshot_id"] is None
    assert row["port_mapping"][22555] == _ENDPOINT["host_port"]
    assert [item["event"] for item in row["state_history"]] == ["stop", "restart", "alive"]
    assert [item["to_state"] for item in row["state_history"]] == ["stopped", "pending", "running"]
    manager._operator.restart.assert_awaited_once()
    manager._operator.submit.assert_not_awaited()


async def test_repeated_stop_preserves_checkpoint_without_new_capture_or_runtime_rpc(manager):
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    assert await manager.stop("vm") is False
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.STOPPED and row["pause_snapshot_id"] == checkpoint
    manager._snapshot_manager.capture_snapshot.assert_awaited_once()
    manager._operator.stop.assert_awaited_once_with("vm", reason=StopReason.MANUAL)


async def test_pending_upload_never_stops_source_and_retry_reuses_checkpoint(manager):
    manager._snapshot_manager.wait_remote_ready.side_effect = TimeoutError("upload pending")
    with pytest.raises(TimeoutError):
        await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    manager._operator.stop.assert_not_awaited()
    assert manager._meta_store.rows["vm"]["state"] == State.RUNNING
    manager._snapshot_manager.get_snapshot_record.side_effect = None
    manager._snapshot_manager.get_snapshot_record.return_value = {"snapshot_id": checkpoint, "state": "LOCAL_READY"}
    manager._snapshot_manager.wait_remote_ready.side_effect = None
    assert await manager.stop("vm") is True
    assert manager._meta_store.rows["vm"]["pause_snapshot_id"] == checkpoint
    manager._snapshot_manager.capture_snapshot.assert_awaited_once()


async def test_failed_checkpoint_is_replaced_on_manual_stop_retry(manager):
    manager._snapshot_manager.wait_remote_ready.side_effect = SnapshotConflict("checkpoint upload failed")
    with pytest.raises(SnapshotConflict, match="checkpoint upload failed"):
        await manager.stop("vm")
    failed_checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    assert manager._meta_store.rows["vm"]["state"] == State.RUNNING
    manager._operator.stop.assert_not_awaited()
    manager._snapshot_manager.get_snapshot_record.side_effect = None
    manager._snapshot_manager.get_snapshot_record.return_value = {
        "snapshot_id": failed_checkpoint, "state": "FAILED",
    }
    manager._snapshot_manager.wait_remote_ready.side_effect = None

    assert await manager.stop("vm") is True

    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    assert checkpoint != failed_checkpoint
    assert manager._snapshot_manager.capture_snapshot.await_count == 2
    assert manager._snapshot_manager.capture_snapshot.await_args.args[1].snapshot_id == checkpoint
    manager._snapshot_manager.wait_remote_ready.assert_awaited_with(checkpoint)
    manager._operator.stop.assert_awaited_once()
    assert manager._meta_store.rows["vm"]["state"] == State.STOPPED


async def test_unknown_checkpoint_capture_is_observed_without_another_cut(manager):
    manager._snapshot_manager.capture_snapshot.side_effect = SnapshotResultUnknown("cut reply lost")
    with pytest.raises(SnapshotResultUnknown, match="cut reply lost"):
        await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    manager._snapshot_manager.get_snapshot_record.side_effect = None
    manager._snapshot_manager.get_snapshot_record.return_value = {"snapshot_id": checkpoint, "state": "CREATING"}

    assert await manager.stop("vm") is True

    assert manager._meta_store.rows["vm"]["pause_snapshot_id"] == checkpoint
    manager._snapshot_manager.capture_snapshot.assert_awaited_once()
    manager._snapshot_manager.wait_remote_ready.assert_awaited_once_with(checkpoint)


async def test_failed_stop_does_not_claim_stopped(manager):
    manager._operator.stop.side_effect = RuntimeError("unknown stop")
    with pytest.raises(RuntimeError, match="unknown stop"):
        await manager.stop("vm")
    assert manager._meta_store.rows["vm"]["state"] == State.RUNNING
    assert manager._meta_store.rows["vm"]["pause_snapshot_id"]


def _lifecycle_observation(manager, code):
    return replace(
        _Operator(manager._meta_store).submit_result,
        sandbox_id="vm",
        state="PENDING" if code in {OperatorResultCode.RESULT_UNKNOWN, OperatorResultCode.ACCEPTED} else "FAILED",
        code=code,
        guest_endpoint=None,
        vmm_pid=None,
        rocklet_artifact=None,
        last_error="actor outcome",
    )


@pytest.mark.parametrize(
    "code,exception",
    [(OperatorResultCode.RESULT_UNKNOWN, SnapshotResultUnknown), (OperatorResultCode.FAILED, SnapshotConflict)],
)
async def test_stop_translates_actor_failure_without_claiming_stopped(manager, code, exception):
    result = _lifecycle_observation(manager, code)
    manager._operator.stop.side_effect = OperatorLifecycleError(sandbox_info_from_result(result))
    with pytest.raises(exception, match="actor outcome"):
        await manager.stop("vm")
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.RUNNING and row["pause_snapshot_id"]


@pytest.mark.parametrize("operation", ["restart", "get_status"])
@pytest.mark.parametrize(
    "code,exception,state",
    [
        (OperatorResultCode.RESULT_UNKNOWN, SnapshotResultUnknown, State.PENDING),
        (OperatorResultCode.FAILED, SnapshotConflict, State.STOPPED),
    ],
)
async def test_restart_translates_actor_failure_and_preserves_checkpoint(manager, operation, code, exception, state):
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    result = _lifecycle_observation(manager, code)
    getattr(manager._operator, operation).side_effect = OperatorLifecycleError(sandbox_info_from_result(result))
    manager._persist_operator_observation = AsyncMock(wraps=manager._persist_operator_observation)
    with pytest.raises(exception, match="actor outcome"):
        await manager.restart_async("vm")
    row = manager._meta_store.rows["vm"]
    assert row["state"] == state and row["pause_snapshot_id"] == checkpoint
    assert row["source_snapshot_id"] == row["spec"]["source_snapshot_id"] == checkpoint
    assert row["host_ip"] == "10.0.0.1" and row["snapd_node_id"] == "node-a"
    if code == OperatorResultCode.RESULT_UNKNOWN:
        assert manager._persist_operator_observation.await_args.args[1] == result


@pytest.mark.parametrize("operation", ["stop", "restart_async"])
async def test_checkpoint_operations_translate_actor_discovery_unknown(manager, operation):
    if operation == "stop":
        manager._operator.stop.side_effect = SnapdResultUnknown("Ray discovery unavailable")
        expected_state = State.RUNNING
    else:
        await manager.stop("vm")
        manager._operator.restart.side_effect = SnapdResultUnknown("Ray discovery unavailable")
        expected_state = State.PENDING
    with pytest.raises(SnapshotResultUnknown, match="Ray discovery unavailable"):
        await getattr(manager, operation)("vm")
    assert manager._meta_store.rows["vm"]["state"] == expected_state
    assert manager._meta_store.rows["vm"]["pause_snapshot_id"]


async def test_unknown_restart_retry_observes_pending_actor_without_resubmission(manager):
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    manager._operator.restart.side_effect = SnapdResultUnknown("lost restart reply")
    with pytest.raises(SnapshotResultUnknown):
        await manager.restart_async("vm")
    assert manager._meta_store.rows["vm"]["state"] == State.PENDING
    response = await manager.restart_async("vm", timeout_seconds=37)
    assert response.sandbox_id == "vm"
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.RUNNING and row["pause_snapshot_id"] is None
    assert row["source_snapshot_id"] == checkpoint
    manager._operator.restart.assert_awaited_once()


async def test_unknown_restart_background_reconciliation_preserves_timeout_without_connect_retry(manager):
    await manager.stop("vm")
    before = int(time.time())

    async def lost_reply(config, *, host_ip):
        durable = manager._meta_store.rows["vm"]
        assert durable["state"] == State.PENDING
        assert durable["source_snapshot_id"] == durable["pause_snapshot_id"]
        assert config.source_snapshot_id == durable["pause_snapshot_id"]
        assert manager._meta_store.timeout[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "1"
        raise SnapdResultUnknown("lost restart reply")

    manager._operator.restart.side_effect = lost_reply
    with pytest.raises(SnapshotResultUnknown):
        await manager.restart_async("vm", timeout_seconds=37)
    timeout = dict(manager._meta_store.timeout)
    assert before + 37 <= int(timeout[env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY]) <= int(time.time()) + 37
    manager._meta_store.update_timeout.assert_awaited_once()
    sm = await manager._get_current_statemachine("vm")
    await manager._try_advance_pending("vm", sm)
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.RUNNING and row["pause_snapshot_id"] is None
    assert manager._meta_store.timeout == timeout
    manager._operator.restart.assert_awaited_once()
    manager._meta_store.update_timeout.assert_awaited_once()


async def test_stop_after_unknown_restart_kills_uncertain_actor_and_retains_checkpoint(manager):
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    manager._operator.restart.side_effect = SnapdResultUnknown("lost restart reply")
    with pytest.raises(SnapshotResultUnknown):
        await manager.restart_async("vm")
    assert await manager.stop("vm") is True
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.STOPPED and row["pause_snapshot_id"] == checkpoint
    assert manager._operator.stop.await_count == 2
    manager._snapshot_manager.capture_snapshot.assert_awaited_once()


async def test_restart_readiness_timeout_remains_unknown_and_retains_pending_admission(manager):
    await manager.stop("vm")
    pending = _lifecycle_observation(manager, OperatorResultCode.ACCEPTED)
    manager._operator.get_status.return_value = sandbox_info_from_result(pending)
    manager._snapshot_template_start_timeout_seconds = 0
    manager._persist_operator_observation = AsyncMock(wraps=manager._persist_operator_observation)
    with pytest.raises(SnapshotResultUnknown, match="readiness timed out"):
        await manager.restart_async("vm")
    assert manager._persist_operator_observation.await_args.args[1] == pending
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.PENDING and row["pause_snapshot_id"]


async def test_failed_restart_status_keeps_checkpoint_and_returns_conflict(manager):
    await manager.stop("vm")
    manager._operator.get_status.return_value = sandbox_info_from_result(
        _lifecycle_observation(manager, OperatorResultCode.FAILED)
    )
    with pytest.raises(SnapshotConflict, match="Restart failed"):
        await manager.restart_async("vm")
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.STOPPED and row["pause_snapshot_id"]


@pytest.mark.parametrize("prior_offset,requested,expected_offset", [(3600, 10, 3600), (-1, 37, 37)])
async def test_restart_running_only_extends_ttl_exactly_without_shortening(
    manager, prior_offset, requested, expected_offset
):
    now = 1_800_000_000
    manager._meta_store.timeout = {env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(now + prior_offset)}
    with patch("rock.sandbox.sandbox_manager.time.time", return_value=now):
        response = await manager.restart_async("vm", timeout_seconds=requested)
    assert response.sandbox_id == "vm"
    timeout = manager._meta_store.update_timeout.await_args.args[1]
    assert int(timeout[env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY]) == now + expected_offset
    assert int(timeout[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY]) == (requested + 59) // 60
    manager._operator.restart.assert_not_awaited()
    manager._operator.submit.assert_not_awaited()


async def test_checkpoint_restart_uses_requested_seconds_instead_of_original_config_minutes(manager):
    await manager.stop("vm")
    now = 1_800_000_000
    with patch("rock.sandbox.sandbox_manager.time.time", return_value=now):
        await manager.restart_async("vm", timeout_seconds=37)
    assert manager._meta_store.update_timeout.await_count == 3
    for call in manager._meta_store.update_timeout.await_args_list:
        assert call.args[1] == {
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(now + 37),
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: "1",
        }


async def test_incomplete_stop_does_not_restart_running_source(manager):
    manager._snapshot_manager.wait_remote_ready.side_effect = TimeoutError("upload pending")
    with pytest.raises(TimeoutError):
        await manager.stop("vm")
    with pytest.raises(SnapshotConflict, match="incomplete"):
        await manager.restart_async("vm")
    manager._operator.restart.assert_not_awaited()
    manager._meta_store.update_timeout.assert_not_awaited()


async def test_stopped_status_requires_all_states_and_does_not_probe_runtime(manager):
    await manager.stop("vm")
    manager._operator.get_status.side_effect = AssertionError("must not probe stopped checkpoint VM")
    with pytest.raises(BadRequestRockError, match="not found"):
        await manager.get_status("vm")
    status = await manager.get_status("vm", include_all_states=True)
    assert status.state == State.STOPPED and not status.is_alive
    assert status.auto_stop_time is None and status.auto_delete_time is None
    assert status.stop_time


async def test_stopped_native_status_uses_retained_checkpoint_size_summary(manager):
    await manager.stop("vm")
    checkpoint = manager._meta_store.rows["vm"]["pause_snapshot_id"]
    manager._snapshot_manager.require_launchable_snapshot.return_value = LaunchableSnapshot(
        snapshot_id=checkpoint,
        kind="FULL",
        format_version=1,
        spec={"backend_v2": "opaque"},
        spec_checksum="test",
        resource_spec={"rootfs_bytes": 128 * 1024 * 1024},
    )
    status = await manager.get_status("vm", include_all_states=True)
    assert status.disk == "134217728B"
    manager._snapshot_manager.get_launchable_snapshot.assert_awaited_once_with(checkpoint)


async def test_status_does_not_require_source_snapshot_publication(manager):
    manager._snapshot_manager.require_launchable_snapshot.side_effect = SnapshotConflict("not REMOTE_READY")
    await manager.stop("vm")
    status = await manager.get_status("vm", include_all_states=True)
    assert status.state == State.STOPPED and status.disk is None


async def test_stopped_delete_releases_checkpoint_reference(manager):
    await manager.stop("vm")
    await manager.delete("vm")
    assert manager._meta_store.rows["vm"]["state"] == State.DELETED
    assert manager._meta_store.rows["vm"]["pause_snapshot_id"] is None
    manager._operator.delete.assert_awaited_once()
    assert "runtime" not in manager._operator.delete.await_args.kwargs


async def test_manual_archive_rejects_retained_checkpoint_before_transition(manager):
    await manager.stop("vm")
    manager._dir_storage = MagicMock()
    manager._image_storage = MagicMock()
    history = list(manager._meta_store.rows["vm"]["state_history"])
    with pytest.raises(BadRequestRockError, match="retained checkpoint"):
        await manager.archive_sandbox("vm")
    row = manager._meta_store.rows["vm"]
    assert row["state"] == State.STOPPED and row["state_history"] == history
    manager._operator.archive.assert_not_awaited()


@pytest.mark.parametrize("checkpoint_runtime", [False, True])
async def test_immediate_delete_policy_is_bypassed_only_for_checkpoint_stop(manager, checkpoint_runtime):
    snapshots = manager._snapshot_manager
    if not checkpoint_runtime:
        manager._snapshot_manager = None
    row = manager._meta_store.rows["vm"]
    row["auto_delete_seconds"] = 0
    row["spec"]["remove_container"] = True
    await manager.stop("vm")
    assert manager._meta_store.rows["vm"]["state"] == (State.STOPPED if checkpoint_runtime else State.DELETED)
    assert bool(manager._meta_store.rows["vm"].get("pause_snapshot_id")) is checkpoint_runtime
    assert snapshots.capture_snapshot.await_count == int(checkpoint_runtime)


@pytest.mark.parametrize("immediate_delete", [False, True])
async def test_expired_stop_preserves_existing_cleanup_without_new_checkpoint(manager, immediate_delete):
    row = manager._meta_store.rows["vm"]
    row["pause_snapshot_id"] = "previous-checkpoint-intent"
    row["auto_delete_seconds"] = 0 if immediate_delete else None
    row["spec"]["remove_container"] = immediate_delete
    await manager.stop("vm", reason=StopReason.EXPIRED)
    row = manager._meta_store.rows["vm"]
    assert row["state"] == (State.DELETED if immediate_delete else State.STOPPED)
    assert row["pause_snapshot_id"] is None
    manager._snapshot_manager.capture_snapshot.assert_not_awaited()
    manager._operator.stop.assert_awaited_once_with("vm", reason=StopReason.EXPIRED)


async def test_non_snapshot_stop_restart_keeps_existing_pending_contract(manager):
    snapshots = manager._snapshot_manager
    manager._snapshot_manager = None
    row = manager._meta_store.rows["vm"]
    row.pop("source_snapshot_id")
    row["spec"]["source_snapshot_id"] = None
    await manager.stop("vm")
    assert manager._meta_store.rows["vm"]["state"] == State.STOPPED
    response = await manager.restart_async("vm")
    assert response.sandbox_id == "vm"
    assert manager._meta_store.rows["vm"]["state"] == State.PENDING
    snapshots.capture_snapshot.assert_not_awaited()
    manager._operator.restart.assert_awaited_once()
    assert "runtime" not in manager._operator.restart.await_args.kwargs
    manager._operator.stop.assert_awaited_once_with("vm", reason=StopReason.MANUAL)
    manager._operator.get_status.assert_not_awaited()
