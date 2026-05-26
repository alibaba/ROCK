"""Tests for SandboxManager lifecycle methods.

Verifies that stop(), get_status(), and start_async() behave correctly for
each sandbox state, using lightweight mocks (no Ray / Docker).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import StopReason
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError


@pytest.fixture
def mock_meta_store():
    store = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.create = AsyncMock()
    store.update = AsyncMock()
    store.archive = AsyncMock()
    store.get_timeout = AsyncMock(return_value=None)
    store.update_timeout = AsyncMock()
    return store


@pytest.fixture
def mock_operator():
    op = AsyncMock()
    op.submit = AsyncMock(return_value={"host_name": "h1", "host_ip": "1.2.3.4", "memory": "1g", "cpus": 1.0})
    op.stop = AsyncMock()
    op.get_status = AsyncMock(return_value={"state": State.RUNNING})
    return op


@pytest.fixture
async def mgr(mock_meta_store, mock_operator):
    """Minimal SandboxManager with real lifecycle logic and mocked infrastructure."""
    from rock.sandbox.sandbox_statemachine import SandboxStateMachine

    m = MagicMock(spec=SandboxManager)
    m._meta_store = mock_meta_store
    m._operator = mock_operator

    m._aes_encrypter = MagicMock()
    m._aes_encrypter.encrypt = MagicMock(return_value="enc")
    m.refresh_aes_key = AsyncMock()

    # Function to get state machine based on meta_store data — mirrors _get_current_statemachine
    async def get_current_statemachine(sandbox_id: str) -> SandboxStateMachine | None:
        info = await mock_meta_store.get(sandbox_id, check_db=True)
        if info is None:
            return None
        state = info.get("state")
        if state is None:
            raise InternalServerRockError(f"Sandbox {sandbox_id} exists in store but has no state field")
        return await SandboxStateMachine.from_state_value(state, sandbox_info=info)

    m._get_current_statemachine = AsyncMock(side_effect=get_current_statemachine)

    m.stop = SandboxManager.stop.__get__(m, SandboxManager)
    m.get_status = SandboxManager.get_status.__get__(m, SandboxManager)
    m._refresh_timeout = AsyncMock()
    return m


# ---------------------------------------------------------------------------
# TestManagerStop
# ---------------------------------------------------------------------------


class TestManagerStop:
    @pytest.mark.asyncio
    async def test_stop_not_found_attempts_cleanup(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = None
        await mgr.stop("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.MANUAL)
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_not_found_actor_missing_still_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = None
        mock_operator.stop.side_effect = ValueError("actor not found")
        await mgr.stop("sb-1")
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_already_stopped_is_noop(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.STOPPED}
        await mgr.stop("sb-1")
        mock_operator.stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_running_calls_operator_and_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        await mgr.stop("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.MANUAL)
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_pending_calls_operator_and_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.PENDING}
        await mgr.stop("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.MANUAL)
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_actor_not_found_still_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        mock_operator.stop.side_effect = ValueError("actor not found")
        await mgr.stop("sb-1")
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_sets_billing_info_when_start_time_present(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING, "start_time": "2024-01-01T00:00:00"}
        with patch("rock.sandbox.sandbox_statemachine.log_billing_info") as mock_billing:
            await mgr.stop("sb-1")
            mock_billing.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_archived_info_has_stopped_state(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        await mgr.stop("sb-1")
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.STOPPED

    @pytest.mark.asyncio
    async def test_stop_missing_state_field_raises(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = {}
        with pytest.raises(InternalServerRockError, match="no state field"):
            await mgr.stop("sb-1")


# ---------------------------------------------------------------------------
# TestManagerGetStatus
# ---------------------------------------------------------------------------


class TestManagerGetStatus:
    @pytest.mark.asyncio
    async def test_not_found_raises_when_operator_returns_none(self, mgr, mock_meta_store, mock_operator):
        mock_operator.get_status.return_value = None
        mock_meta_store.get.return_value = None
        with pytest.raises(BadRequestRockError, match="not found"):
            await mgr.get_status("sb-1")

    @pytest.mark.asyncio
    async def test_include_all_states_falls_back_to_meta_store(self, mgr, mock_meta_store, mock_operator):
        mock_operator.get_status.return_value = None
        mock_meta_store.get.return_value = {"state": State.STOPPED, "phases": {}, "port_mapping": {}}
        result = await mgr.get_status("sb-1", include_all_states=True)
        assert result.state == State.STOPPED
        assert result.is_alive is False
        mock_meta_store.get.assert_awaited_with("sb-1", check_db=True)

    @pytest.mark.asyncio
    async def test_running_returns_response(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        mock_operator.get_status.return_value = {
            "state": State.RUNNING,
            "host_name": "h1",
            "host_ip": "1.2.3.4",
            "phases": {},
            "port_mapping": {},
        }
        result = await mgr.get_status("sb-1")
        assert result.sandbox_id == "sb-1"
        assert result.is_alive is True

    @pytest.mark.asyncio
    async def test_updates_meta_on_state_change(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.PENDING}
        mock_operator.get_status.return_value = {"state": State.RUNNING, "phases": {}, "port_mapping": {}}
        await mgr.get_status("sb-1")
        mock_meta_store.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_update_if_state_unchanged(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        mock_operator.get_status.return_value = {"state": State.RUNNING, "phases": {}, "port_mapping": {}}
        await mgr.get_status("sb-1")
        mock_meta_store.update.assert_not_awaited()
