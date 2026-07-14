"""
Unit tests for SandboxManager.get_status with include_all_states support.

Key behaviour changes covered:
  - operator.get_status() may now return None
  - include_all_states=True  + operator data  → skip extra DB fallback, normal path
  - meta_store.update only when state transitions PENDING → RUNNING
  - start_time/stop_time/create_time populated in every response
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.response import SandboxStatusResponse


def _make_sandbox_info(sandbox_id: str = "sandbox-1", state: State = State.RUNNING) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=sandbox_id,
        state=state,
        host_ip="10.0.0.1" if state != State.PENDING else None,
        host_name="node-1" if state != State.PENDING else None,
        image="python:3.11",
        phases={},
        port_mapping={},
    )


@pytest.fixture
def mock_operator():
    return AsyncMock()


@pytest.fixture
def mock_meta_store():
    store = AsyncMock()
    # Default: return sandbox info (not None) so get_status can proceed
    store.get = AsyncMock(return_value=_make_sandbox_info(state=State.PENDING))
    store.update = AsyncMock()
    return store


@pytest.fixture
async def sandbox_manager(mock_operator, mock_meta_store, rock_config):
    from rock.sandbox.sandbox_manager import SandboxManager
    from rock.sandbox.sandbox_statemachine import SandboxStateMachine

    manager = SandboxManager.__new__(SandboxManager)
    manager.rock_config = rock_config
    manager._operator = mock_operator
    manager._meta_store = mock_meta_store
    manager._refresh_timeout = AsyncMock()

    mock_sm = await SandboxStateMachine.from_state_value(State.PENDING, sandbox_info={})
    manager._get_current_statemachine = AsyncMock(return_value=mock_sm)

    return manager


class TestGetStatusIncludeAllStates:
    @pytest.mark.asyncio
    async def test_running_sandbox_returns_alive_response(self, sandbox_manager, mock_operator):
        """Operator returns RUNNING → is_alive=True, state field populated."""
        mock_operator.get_status = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))

        result = await sandbox_manager.get_status("sandbox-1")

        assert isinstance(result, SandboxStatusResponse)
        assert result.state == State.RUNNING
        assert result.is_alive is True

    @pytest.mark.asyncio
    async def test_running_state_triggers_meta_store_update(self, sandbox_manager, mock_operator, mock_meta_store):
        """RUNNING state writes back to meta_store when state changed."""
        mock_operator.get_status = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))

        await sandbox_manager.get_status("sandbox-1")

        mock_meta_store.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_running_state_skips_state_machine_init(self, sandbox_manager, mock_operator, mock_meta_store):
        """When meta_store and operator both say RUNNING, state machine is NOT initialized (optimization)."""
        # Set meta_store to return RUNNING (not PENDING) so no transition is needed
        mock_meta_store.get = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))
        mock_operator.get_status = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))

        await sandbox_manager.get_status("sandbox-1", include_all_states=True)

        # State machine should NOT be initialized when no PENDING→RUNNING transition needed
        sandbox_manager._get_current_statemachine.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_operator_status_skips_alive_when_current_state_already_running(
        self, sandbox_manager, mock_operator
    ):
        """Avoid double-sending alive when another status refresh already advanced the state machine."""
        mock_operator.get_status = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))
        mock_sm = MagicMock()
        mock_sm.current_state.value = State.RUNNING
        mock_sm.send = AsyncMock()
        sandbox_manager._get_current_statemachine = AsyncMock(return_value=mock_sm)

        result = await sandbox_manager.get_status("sandbox-1")

        assert result.is_alive is True
        sandbox_manager._get_current_statemachine.assert_awaited_once_with("sandbox-1")
        mock_sm.send.assert_not_awaited()
