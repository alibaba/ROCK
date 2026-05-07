"""Tests for sandbox transition-table dispatch logic.

These tests use lightweight mocks (no Ray / Docker) to verify:
- Transition table completeness and rejection of unknown pairs
- get_transition_handler routes to correct handler
- Handler behavior for stop, get_status, start
- End-to-end idempotent stop via SandboxManager.stop()
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.sandbox.sandbox_manager import _NOT_EXIST, TRANSITION_MAP, SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def mgr(mock_meta_store, mock_operator):
    """A minimal mock SandboxManager with real dispatch logic."""
    m = MagicMock(spec=SandboxManager)
    m._meta_store = mock_meta_store
    m._operator = mock_operator
    m._build_sandbox_info_metadata = SandboxManager._build_sandbox_info_metadata.__get__(m)
    m.refresh_aes_key = AsyncMock()
    m._aes_encrypter = MagicMock()
    m._aes_encrypter.encrypt = MagicMock(return_value="enc")
    m.get_transition_handler = SandboxManager.get_transition_handler.__get__(m)
    m._handle_start = SandboxManager._handle_start.__get__(m)
    m._handle_stop = SandboxManager._handle_stop.__get__(m)
    m._handle_stop_noop = SandboxManager._handle_stop_noop.__get__(m)
    m._handle_get_status = SandboxManager._handle_get_status.__get__(m)
    m._handle_get_status_stopped = SandboxManager._handle_get_status_stopped.__get__(m)
    m._handle_get_status_not_found = SandboxManager._handle_get_status_not_found.__get__(m)
    m._refresh_timeout = AsyncMock()
    return m


# ---------------------------------------------------------------------------
# TestTransitionTable — table completeness
# ---------------------------------------------------------------------------


class TestTransitionTable:
    def test_all_states_have_stop(self):
        for state in [State.PENDING, State.RUNNING, State.STOPPED, _NOT_EXIST]:
            assert (state, "stop") in TRANSITION_MAP

    def test_all_states_have_get_status(self):
        for state in [State.PENDING, State.RUNNING, State.STOPPED, _NOT_EXIST]:
            assert (state, "get_status") in TRANSITION_MAP

    def test_start_only_for_not_exist(self):
        assert (_NOT_EXIST, "start") in TRANSITION_MAP
        assert (State.PENDING, "start") not in TRANSITION_MAP
        assert (State.RUNNING, "start") not in TRANSITION_MAP
        assert (State.STOPPED, "start") not in TRANSITION_MAP

    def test_all_handlers_exist_on_manager(self):
        for handler_name in TRANSITION_MAP.values():
            assert hasattr(SandboxManager, handler_name), f"{handler_name} not found on SandboxManager"


# ---------------------------------------------------------------------------
# State resolution (inlined in get_transition_handler)
# ---------------------------------------------------------------------------


class TestTransitionHandlerStateResolution:
    @pytest.mark.asyncio
    async def test_not_exist_routes_stop_to_noop(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = None
        handler = await mgr.get_transition_handler("sb-1", "stop")
        assert handler.__func__ is SandboxManager._handle_stop_noop

    @pytest.mark.asyncio
    async def test_running_routes_stop_to_handle_stop(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        handler = await mgr.get_transition_handler("sb-1", "stop")
        assert handler.__func__ is SandboxManager._handle_stop

    @pytest.mark.asyncio
    async def test_missing_state_field_raises(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = {}
        with pytest.raises(InternalServerRockError, match="no state field"):
            await mgr.get_transition_handler("sb-1", "stop")


# ---------------------------------------------------------------------------
# TestDispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.asyncio
    async def test_unknown_state_action_raises(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = {"state": State.STOPPED}
        with pytest.raises(BadRequestRockError, match="not allowed"):
            await mgr.get_transition_handler("sb-1", "start")

    @pytest.mark.asyncio
    async def test_unknown_state_value_raises(self, mgr, mock_meta_store):
        mock_meta_store.get.return_value = {"state": "some_invalid_state"}
        with pytest.raises(BadRequestRockError, match="not allowed"):
            await mgr.get_transition_handler("sb-1", "stop")

    @pytest.mark.asyncio
    async def test_routes_to_correct_handler(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING, "start_time": "2024-01-01T00:00:00"}
        handler = await mgr.get_transition_handler("sb-1", "stop")
        await handler("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1")


# ---------------------------------------------------------------------------
# TestHandleStop
# ---------------------------------------------------------------------------


class TestHandleStop:
    @pytest.mark.asyncio
    async def test_stop_sets_stopped_and_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        await mgr._handle_stop("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1")
        mock_meta_store.archive.assert_awaited_once()
        archived_info = mock_meta_store.archive.call_args[0][1]
        assert archived_info["state"] == State.STOPPED

    @pytest.mark.asyncio
    async def test_stop_with_billing(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING, "start_time": "2024-01-01T00:00:00"}
        with patch("rock.sandbox.sandbox_manager.log_billing_info") as mock_billing:
            await mgr._handle_stop("sb-1")
            mock_billing.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_actor_not_found_still_archives(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        mock_operator.stop.side_effect = ValueError("actor not found")
        await mgr._handle_stop("sb-1")
        mock_meta_store.archive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_sandbox_not_in_redis(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = None
        await mgr._handle_stop("sb-1")
        mock_operator.stop.assert_awaited_once()
        mock_meta_store.archive.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestHandleStopNoop
# ---------------------------------------------------------------------------


class TestHandleStopNoop:
    @pytest.mark.asyncio
    async def test_stopped_sandbox_noop(self, mgr, mock_operator):
        await mgr._handle_stop_noop("sb-1")
        mock_operator.stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_found_sandbox_noop(self, mgr, mock_operator):
        await mgr._handle_stop_noop("sb-gone")
        mock_operator.stop.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestHandleGetStatus
# ---------------------------------------------------------------------------


class TestHandleGetStatus:
    @pytest.mark.asyncio
    async def test_returns_response(self, mgr, mock_meta_store, mock_operator):
        mock_operator.get_status.return_value = {
            "state": State.RUNNING,
            "host_name": "h1",
            "host_ip": "1.2.3.4",
            "phases": {},
            "port_mapping": {},
        }
        mock_meta_store.get.return_value = {"state": State.PENDING}
        result = await mgr._handle_get_status("sb-1")
        assert result.sandbox_id == "sb-1"
        assert result.is_alive is True

    @pytest.mark.asyncio
    async def test_stopped_raises(self, mgr, mock_operator):
        mock_operator.get_status.return_value = {"state": State.STOPPED}
        with pytest.raises(BadRequestRockError, match="already stopped"):
            await mgr._handle_get_status("sb-1")

    @pytest.mark.asyncio
    async def test_updates_meta_on_state_change(self, mgr, mock_meta_store, mock_operator):
        mock_operator.get_status.return_value = {"state": State.RUNNING, "phases": {}, "port_mapping": {}}
        mock_meta_store.get.return_value = {"state": State.PENDING}
        await mgr._handle_get_status("sb-1")
        mock_meta_store.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_update_if_same_state(self, mgr, mock_meta_store, mock_operator):
        mock_operator.get_status.return_value = {"state": State.RUNNING, "phases": {}, "port_mapping": {}}
        mock_meta_store.get.return_value = {"state": State.RUNNING}
        await mgr._handle_get_status("sb-1")
        mock_meta_store.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestHandleGetStatusEdgeCases
# ---------------------------------------------------------------------------


class TestHandleGetStatusEdgeCases:
    @pytest.mark.asyncio
    async def test_stopped_state_raises(self, mgr):
        with pytest.raises(BadRequestRockError, match="already stopped"):
            await mgr._handle_get_status_stopped("sb-1")

    @pytest.mark.asyncio
    async def test_not_found_raises(self, mgr):
        with pytest.raises(BadRequestRockError, match="not found"):
            await mgr._handle_get_status_not_found("sb-1")


# ---------------------------------------------------------------------------
# TestHandleStart
# ---------------------------------------------------------------------------


class TestHandleStart:
    @pytest.mark.asyncio
    async def test_creates_and_returns_response(self, mgr, mock_meta_store, mock_operator):
        docker_cfg = MagicMock()
        docker_cfg.auto_clear_time = 30
        result = await mgr._handle_start(
            "sb-new",
            docker_deployment_config=docker_cfg,
            user_info={},
            cluster_info={},
        )
        assert result.sandbox_id == "sb-new"
        assert result.host_name == "h1"
        mock_operator.submit.assert_awaited_once_with(docker_cfg, {})
        mock_meta_store.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestIdempotentStop — end-to-end via SandboxManager.stop()
# ---------------------------------------------------------------------------


class TestIdempotentStop:
    @pytest.mark.asyncio
    async def test_stop_already_archived_sandbox(self):
        m = MagicMock(spec=SandboxManager)
        m._meta_store = AsyncMock()
        m._meta_store.get = AsyncMock(return_value=None)
        m._operator = AsyncMock()
        m.get_transition_handler = SandboxManager.get_transition_handler.__get__(m)
        m._handle_stop_noop = SandboxManager._handle_stop_noop.__get__(m)
        m.stop = SandboxManager.stop.__wrapped__.__get__(m)

        await m.stop("already-stopped-sb")
        m._operator.stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_running_still_works(self):
        m = MagicMock(spec=SandboxManager)
        m._meta_store = AsyncMock()
        m._meta_store.get = AsyncMock(return_value={"state": State.RUNNING, "start_time": "2024-01-01T00:00:00"})
        m._meta_store.archive = AsyncMock()
        m._operator = AsyncMock()
        m.get_transition_handler = SandboxManager.get_transition_handler.__get__(m)
        m._handle_stop = SandboxManager._handle_stop.__get__(m)
        m.stop = SandboxManager.stop.__wrapped__.__get__(m)

        await m.stop("sb-running")
        m._operator.stop.assert_awaited_once_with("sb-running")
