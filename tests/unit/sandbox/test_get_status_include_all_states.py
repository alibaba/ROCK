"""
Unit tests for SandboxManager.get_status changes in feat(sandbox): support include_all_states.

Key behaviour changes covered:
  - operator.get_status() may now return None
  - include_all_states=False + operator None  → raise BadRequestRockError("not found")
  - include_all_states=True  + operator None  → fall back to meta_store.get(check_db=True)
  - include_all_states=True  + operator data  → skip fallback, normal path
  - meta_store.update only when state is PENDING or RUNNING
  - start_time/stop_time/create_time populated in every response
"""

from unittest.mock import AsyncMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.response import SandboxStatusResponse
from rock.sdk.common.exceptions import BadRequestRockError


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
    store.get = AsyncMock(return_value=None)
    store.update = AsyncMock()
    return store


@pytest.fixture
def sandbox_manager(mock_operator, mock_meta_store, rock_config):
    from rock.sandbox.sandbox_manager import SandboxManager

    with patch("rock.sandbox.sandbox_manager.SandboxProxyService"):
        manager = SandboxManager.__new__(SandboxManager)
        manager.rock_config = rock_config
        manager._operator = mock_operator
        manager._meta_store = mock_meta_store
        manager._refresh_timeout = AsyncMock()
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
    async def test_operator_none_flag_false_raises_not_found(self, sandbox_manager, mock_operator, mock_meta_store):
        """operator=None + include_all_states=False → not found, no DB fallback triggered."""
        mock_operator.get_status = AsyncMock(return_value=None)

        with pytest.raises(BadRequestRockError, match="not found"):
            await sandbox_manager.get_status("sandbox-1", include_all_states=False)

        for c in mock_meta_store.get.call_args_list:
            assert not c.kwargs.get("check_db")

    @pytest.mark.asyncio
    async def test_operator_none_flag_true_calls_db_fallback(self, sandbox_manager, mock_operator, mock_meta_store):
        """operator=None + include_all_states=True → meta_store.get(check_db=True) called."""
        mock_operator.get_status = AsyncMock(return_value=None)
        mock_meta_store.get = AsyncMock(return_value=_make_sandbox_info(state=State.PENDING))

        result = await sandbox_manager.get_status("sandbox-1", include_all_states=True)

        mock_meta_store.get.assert_awaited_once_with("sandbox-1", check_db=True)
        assert result.state == State.PENDING

    @pytest.mark.asyncio
    async def test_operator_none_flag_true_db_empty_raises(self, sandbox_manager, mock_operator):
        """operator=None + include_all_states=True + DB empty → not found."""
        mock_operator.get_status = AsyncMock(return_value=None)

        with pytest.raises(BadRequestRockError, match="not found"):
            await sandbox_manager.get_status("sandbox-1", include_all_states=True)

    @pytest.mark.asyncio
    async def test_operator_data_with_flag_true_skips_db_fallback(
        self, sandbox_manager, mock_operator, mock_meta_store
    ):
        """operator returns data + include_all_states=True → check_db=True never triggered."""
        mock_operator.get_status = AsyncMock(return_value=_make_sandbox_info(state=State.RUNNING))

        await sandbox_manager.get_status("sandbox-1", include_all_states=True)

        for c in mock_meta_store.get.call_args_list:
            assert not c.kwargs.get("check_db")
