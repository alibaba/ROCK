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
