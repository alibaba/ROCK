"""
Unit tests for SandboxStateMachine.

Covers:
- State transitions (valid and invalid)
- State.active properties for querying state
- State restoration via from_state_value()
- Async action callbacks: on_stop
"""

from unittest.mock import AsyncMock, patch

import pytest
from statemachine.exceptions import TransitionNotAllowed

from rock.actions.sandbox.response import State
from rock.common.constants import StopReason
from rock.sandbox.sandbox_statemachine import SandboxStateMachine

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    @pytest.mark.asyncio
    async def test_initial_state_is_pending(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        assert sm.pending.is_active
        assert not sm.running.is_active
        assert not sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_alive_goes_to_running(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        await sm.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        assert sm.running.is_active

    @pytest.mark.asyncio
    async def test_stop_from_pending(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        await sm.send("stop", sandbox_id="sb", operator=AsyncMock(), meta_store=AsyncMock())
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_stop_from_running(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        await sm.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        await sm.send("stop", sandbox_id="sb", operator=AsyncMock(), meta_store=AsyncMock())
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_stop_noop_from_stopped(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        await sm.send("stop", sandbox_id="sb", operator=AsyncMock(), meta_store=AsyncMock())
        await sm.send("stop_noop", sandbox_id="sb")
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        assert sm.pending.is_active
        await sm.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        assert sm.running.is_active
        await sm.send("stop", sandbox_id="sb", operator=AsyncMock(), meta_store=AsyncMock())
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_multiple_instances_are_independent(self):
        sm1, sm2 = SandboxStateMachine(), SandboxStateMachine()
        await sm1.activate_initial_state()
        await sm2.activate_initial_state()
        await sm1.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        assert sm1.running.is_active
        assert sm2.pending.is_active


# ---------------------------------------------------------------------------
# State query helpers
# ---------------------------------------------------------------------------


class TestStateHelpers:
    @pytest.mark.asyncio
    async def test_state_active_properties_track_state(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        assert sm.pending.is_active and not sm.running.is_active

        await sm.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        assert sm.running.is_active

        await sm.send("stop", sandbox_id="sb", operator=AsyncMock(), meta_store=AsyncMock())
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_repr(self):
        sm = SandboxStateMachine()
        await sm.activate_initial_state()
        assert "pending" in repr(sm)
        await sm.send("alive", sandbox_id="sb", meta_store=AsyncMock(), sandbox_info={})
        assert "running" in repr(sm)


# ---------------------------------------------------------------------------
# State restoration
# ---------------------------------------------------------------------------


class TestFromStateValue:
    @pytest.mark.asyncio
    async def test_none_starts_in_pending(self):
        sm = await SandboxStateMachine.from_state_value(None, sandbox_info={})
        assert sm.pending.is_active

    @pytest.mark.asyncio
    async def test_restores_pending(self):
        sm = await SandboxStateMachine.from_state_value(State.PENDING, sandbox_info={})
        assert sm.pending.is_active

    @pytest.mark.asyncio
    async def test_restores_running(self):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        assert sm.running.is_active

    @pytest.mark.asyncio
    async def test_restores_stopped(self):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info={})
        assert sm.stopped.is_active

    @pytest.mark.asyncio
    async def test_unknown_value_defaults_to_pending(self):
        sm = await SandboxStateMachine.from_state_value("bogus", sandbox_info={})
        assert sm.pending.is_active


# ---------------------------------------------------------------------------
# on_stop callback
# ---------------------------------------------------------------------------


class TestOnStop:
    @pytest.fixture
    def mock_operator(self):
        return AsyncMock()

    @pytest.fixture
    def mock_meta_store(self):
        store = AsyncMock()
        store.get = AsyncMock(return_value={"state": State.RUNNING})
        return store

    @pytest.mark.asyncio
    async def test_stops_operator_and_archives(self, mock_operator, mock_meta_store):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        await sm.send("stop", sandbox_id="sb-1", operator=mock_operator, meta_store=mock_meta_store)
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.MANUAL)
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_propagates_reason_to_operator(self, mock_operator, mock_meta_store):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        await sm.send(
            "stop",
            sandbox_id="sb-1",
            operator=mock_operator,
            meta_store=mock_meta_store,
            reason=StopReason.EXPIRED,
        )
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.EXPIRED)

    @pytest.mark.asyncio
    async def test_archives_stopped_state(self, mock_operator, mock_meta_store):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        await sm.send("stop", sandbox_id="sb-1", operator=mock_operator, meta_store=mock_meta_store)
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.STOPPED

    @pytest.mark.asyncio
    async def test_actor_not_found_still_archives(self, mock_meta_store):
        op = AsyncMock()
        op.stop = AsyncMock(side_effect=ValueError("not found"))
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        await sm.send("stop", sandbox_id="sb-1", operator=op, meta_store=mock_meta_store)
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logs_billing_when_start_time_present(self, mock_operator, mock_meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.RUNNING, sandbox_info={"state": State.RUNNING, "start_time": "2024-01-01T00:00:00"}
        )
        with patch("rock.sandbox.sandbox_statemachine.log_billing_info") as mock_billing:
            await sm.send("stop", sandbox_id="sb-1", operator=mock_operator, meta_store=mock_meta_store)
            mock_billing.assert_called_once()

    @pytest.mark.asyncio
    async def test_meta_store_none_still_archives(self, mock_operator):
        store = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        await sm.send("stop", sandbox_id="sb-1", operator=mock_operator, meta_store=store)
        store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_time_always_written_even_when_start_failed(self, mock_operator, mock_meta_store):
        """REGRESSION: sandboxes that fail before sandbox_actor writes start_time
        (image pull / docker run errors) still get stop_time. Without this,
        SandboxLogArchiveTask can't age them and their log dirs leak forever.
        Billing stays gated on start_time (billing only meaningful for started)."""
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})  # no start_time
        with patch("rock.sandbox.sandbox_statemachine.log_billing_info") as mock_billing:
            await sm.send("stop", sandbox_id="sb-failed", operator=mock_operator, meta_store=mock_meta_store)

        archived = mock_meta_store.archive.call_args[0][1]
        assert archived["state"] == State.STOPPED
        assert archived.get("stop_time"), "stop_time must be set even when start_time absent"
        mock_billing.assert_not_called()  # no billing when start_time absent


# ---------------------------------------------------------------------------
# restart transitions
# ---------------------------------------------------------------------------


_VALID_RESTART_INFO = {
    "host_ip": "1.2.3.4",
    "spec": {
        "container_name": "sb",
        "image": "python:3.11",
        "memory": "2g",
        "cpus": 1,
        "auto_clear_time_minutes": 30,
    },
}


def _default_restart_config(**overrides):
    from rock.deployments.config import DockerDeploymentConfig

    kwargs = dict(_VALID_RESTART_INFO["spec"])
    kwargs.update(overrides)
    return DockerDeploymentConfig(**kwargs)


class TestRestartTransitions:
    def _restart_kwargs(self, meta_store=None):
        return dict(
            sandbox_id="sb",
            operator=AsyncMock(),
            meta_store=meta_store or AsyncMock(),
            restart_config=_default_restart_config(),
        )

    @pytest.mark.asyncio
    async def test_restart_from_stopped_transitions_to_pending(self):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=dict(_VALID_RESTART_INFO))
        await sm.send("restart", **self._restart_kwargs())
        assert sm.pending.is_active

    @pytest.mark.asyncio
    async def test_restart_from_running_raises(self):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("restart", **self._restart_kwargs())

    @pytest.mark.asyncio
    async def test_restart_from_pending_raises(self):
        sm = await SandboxStateMachine.from_state_value(State.PENDING, sandbox_info={})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("restart", **self._restart_kwargs())


# ---------------------------------------------------------------------------
# on_restart callback
# ---------------------------------------------------------------------------


class TestOnRestart:
    @pytest.fixture
    def mock_meta_store(self):
        return AsyncMock()

    async def _send_restart(self, mock_meta_store, sandbox_info=None, config=None):
        info = sandbox_info if sandbox_info is not None else dict(_VALID_RESTART_INFO)
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=info)
        await sm.send(
            "restart",
            sandbox_id="sb-1",
            operator=AsyncMock(),
            meta_store=mock_meta_store,
            restart_config=config or _default_restart_config(),
        )

    @pytest.mark.asyncio
    async def test_calls_meta_store_update_not_create(self, mock_meta_store):
        await self._send_restart(mock_meta_store)
        mock_meta_store.update.assert_awaited_once()
        mock_meta_store.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_updates_state_to_pending(self, mock_meta_store):
        await self._send_restart(mock_meta_store)
        updated_info = mock_meta_store.update.call_args[0][1]
        assert updated_info["state"] == State.PENDING

    @pytest.mark.asyncio
    async def test_writes_timeout_built_from_spec(self, mock_meta_store):
        await self._send_restart(mock_meta_store)
        mock_meta_store.update_timeout.assert_awaited_once()
        sandbox_id, timeout_info = mock_meta_store.update_timeout.call_args[0]
        assert sandbox_id == "sb-1"
        assert any("30" == str(v) for v in timeout_info.values())


# ---------------------------------------------------------------------------
# on_restart resource overrides
# ---------------------------------------------------------------------------


class TestOnRestartResourceOverrides:
    @pytest.fixture
    def mock_meta_store(self):
        return AsyncMock()

    async def _send_restart(self, mock_meta_store, config=None, sandbox_info=None):
        info = sandbox_info if sandbox_info is not None else dict(_VALID_RESTART_INFO)
        if "spec" in info:
            info = {**info, "spec": dict(info["spec"])}
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=info)
        mock_operator = AsyncMock()
        await sm.send(
            "restart",
            sandbox_id="sb-1",
            operator=mock_operator,
            meta_store=mock_meta_store,
            restart_config=config or _default_restart_config(),
        )
        return mock_operator

    @pytest.mark.asyncio
    async def test_config_passed_to_operator(self, mock_meta_store):
        config = _default_restart_config(cpus=4)
        mock_operator = await self._send_restart(mock_meta_store, config=config)
        restart_config = mock_operator.restart.call_args[0][0]
        assert restart_config.cpus == 4

    @pytest.mark.asyncio
    async def test_no_change_uses_original_config(self, mock_meta_store):
        mock_operator = await self._send_restart(mock_meta_store)
        restart_config = mock_operator.restart.call_args[0][0]
        assert restart_config.cpus == 1
        assert restart_config.memory == "2g"

    @pytest.mark.asyncio
    async def test_limit_cpus_passed_to_operator(self, mock_meta_store):
        config = _default_restart_config(cpus=4, limit_cpus=6)
        mock_operator = await self._send_restart(mock_meta_store, config=config)
        restart_config = mock_operator.restart.call_args[0][0]
        assert restart_config.cpus == 4
        assert restart_config.limit_cpus == 6

    @pytest.mark.asyncio
    async def test_update_persists_cpus_and_memory(self, mock_meta_store):
        config = _default_restart_config(cpus=8, memory="32g")
        await self._send_restart(mock_meta_store, config=config)
        mock_meta_store.update.assert_awaited_once()
        updated_info = mock_meta_store.update.call_args[0][1]
        assert updated_info["cpus"] == 8
        assert updated_info["memory"] == "32g"
        assert updated_info["state"] == State.PENDING


# ---------------------------------------------------------------------------
# restart resource validation (reuses SandboxManager.validate_sandbox_spec)
# ---------------------------------------------------------------------------


class TestRestartResourceValidation:
    @pytest.fixture
    def sandbox_manager(self):
        from unittest.mock import MagicMock

        from rock.config import RockConfig, RuntimeConfig, StandardSpec
        from rock.sandbox.sandbox_manager import SandboxManager

        mgr = MagicMock()
        mgr.rock_config = MagicMock(spec=RockConfig)
        mgr.rock_config.runtime = MagicMock(spec=RuntimeConfig)
        mgr.rock_config.runtime.max_allowed_spec = StandardSpec(cpus=16, memory="64g")
        mgr.validate_sandbox_spec = SandboxManager.validate_sandbox_spec.__get__(mgr)
        return mgr

    def _make_config(self, cpus=2, memory="8g"):
        from rock.deployments.config import DockerDeploymentConfig

        return DockerDeploymentConfig(container_name="sb-1", image="python:3.11", cpus=cpus, memory=memory)

    def test_cpus_within_limit_passes(self, sandbox_manager):
        sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(cpus=8))

    def test_cpus_exceeds_limit_raises(self, sandbox_manager):
        from rock.sdk.common.exceptions import BadRequestRockError

        with pytest.raises(BadRequestRockError, match="exceed the maximum allowed"):
            sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(cpus=32))

    def test_memory_within_limit_passes(self, sandbox_manager):
        sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(memory="32g"))

    def test_memory_exceeds_limit_raises(self, sandbox_manager):
        from rock.sdk.common.exceptions import BadRequestRockError

        with pytest.raises(BadRequestRockError, match="exceed the maximum allowed"):
            sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(memory="128g"))

    def test_invalid_memory_format_raises(self, sandbox_manager):
        from rock.sdk.common.exceptions import BadRequestRockError

        with pytest.raises(BadRequestRockError, match="Invalid memory size"):
            sandbox_manager.validate_sandbox_spec(
                sandbox_manager.rock_config.runtime, self._make_config(memory="not_a_size")
            )

    def test_both_within_limit_passes(self, sandbox_manager):
        sandbox_manager.validate_sandbox_spec(
            sandbox_manager.rock_config.runtime, self._make_config(cpus=16, memory="64g")
        )

    def test_cpus_at_boundary_passes(self, sandbox_manager):
        sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(cpus=16))

    def test_memory_at_boundary_passes(self, sandbox_manager):
        sandbox_manager.validate_sandbox_spec(sandbox_manager.rock_config.runtime, self._make_config(memory="64g"))


# ---------------------------------------------------------------------------
# delete transitions
# ---------------------------------------------------------------------------


class TestDeleteTransitions:
    def _kwargs(self, operator=None, meta_store=None):
        return dict(
            sandbox_id="sb",
            operator=operator or AsyncMock(),
            meta_store=meta_store or AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_delete_from_stopped_transitions_to_deleted(self):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info={"host_ip": "1.2.3.4"})
        await sm.send("delete", **self._kwargs())
        assert sm.deleted.is_active

    @pytest.mark.asyncio
    async def test_delete_from_pending_raises(self):
        sm = await SandboxStateMachine.from_state_value(State.PENDING, sandbox_info={})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("delete", **self._kwargs())

    @pytest.mark.asyncio
    async def test_delete_from_running_raises(self):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, sandbox_info={})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("delete", **self._kwargs())

    @pytest.mark.asyncio
    async def test_deleted_is_final_no_transitions_allowed(self):
        sm = await SandboxStateMachine.from_state_value(State.DELETED, sandbox_info={})
        for event in ("stop", "stop_noop", "alive", "restart", "delete"):
            with pytest.raises(TransitionNotAllowed):
                await sm.send(event, **self._kwargs(), sandbox_info={})


_VALID_DELETE_INFO = {
    "host_ip": "10.0.0.1",
    "spec": {
        "container_name": "sb-1",
        "image": "python:3.11",
        "memory": "2g",
        "cpus": 1,
        "auto_clear_time_minutes": 30,
    },
}


class TestOnDelete:
    @pytest.fixture
    def mock_meta_store(self):
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_calls_operator_delete_with_config_and_host_ip(self, mock_meta_store):
        op = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=dict(_VALID_DELETE_INFO))
        await sm.send("delete", sandbox_id="sb-1", operator=op, meta_store=mock_meta_store)
        op.delete.assert_awaited_once()
        args, kwargs = op.delete.call_args
        config = args[0]
        assert config.container_name == "sb-1"
        assert kwargs.get("host_ip") == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_archives_with_state_deleted_and_delete_time(self, mock_meta_store):
        op = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=dict(_VALID_DELETE_INFO))
        await sm.send("delete", sandbox_id="sb-1", operator=op, meta_store=mock_meta_store)
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.DELETED
        assert archived_info["delete_time"]

    @pytest.mark.asyncio
    async def test_operator_delete_failure_still_archives(self, mock_meta_store):
        op = AsyncMock()
        op.delete = AsyncMock(side_effect=RuntimeError("worker unreachable"))
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info=dict(_VALID_DELETE_INFO))
        await sm.send("delete", sandbox_id="sb-1", operator=op, meta_store=mock_meta_store)
        mock_meta_store.archive.assert_awaited_once()
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.DELETED

    @pytest.mark.asyncio
    async def test_missing_spec_skips_operator_but_still_archives(self, mock_meta_store):
        """No ``spec`` snapshot → cannot rebuild config → skip operator.delete,
        still soft-delete the record (ContainerCleanupTask becomes fallback)."""
        op = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, sandbox_info={"host_ip": "10.0.0.1"})
        await sm.send("delete", sandbox_id="sb-1", operator=op, meta_store=mock_meta_store)
        op.delete.assert_not_called()
        mock_meta_store.archive.assert_awaited_once()
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.DELETED

    @pytest.mark.asyncio
    async def test_restores_deleted_from_state_value(self):
        sm = await SandboxStateMachine.from_state_value(State.DELETED, sandbox_info={})
        assert sm.deleted.is_active
