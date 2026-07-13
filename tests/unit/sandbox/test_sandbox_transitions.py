"""Tests for SandboxManager lifecycle methods.

Verifies that stop(), get_status(), and start_async() behave correctly for
each sandbox state, using lightweight mocks (no Ray / Docker).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStartResponse
from rock.common.constants import StopReason
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


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
        from rock.sandbox.sandbox_statemachine import SandboxStateMachine

        info = await mock_meta_store.get(sandbox_id, check_db=True)
        if info is None:
            return None
        return await SandboxStateMachine.from_state_value(info.get("state"), sandbox_info=info)

    m._get_current_statemachine = AsyncMock(side_effect=get_current_statemachine)

    m.stop = SandboxManager.stop.__get__(m, SandboxManager)
    m._try_advance_pending = SandboxManager._try_advance_pending.__get__(m, SandboxManager)
    m.get_status = SandboxManager.get_status.__get__(m, SandboxManager)
    m._refresh_timeout = AsyncMock()
    return m


# ---------------------------------------------------------------------------
# TestManagerStop
# ---------------------------------------------------------------------------


class TestManagerStop:
    @pytest.mark.asyncio
    async def test_stop_not_found_attempts_operator_stop(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = None
        await mgr.stop("sb-1")
        mock_operator.stop.assert_awaited_once_with("sb-1", reason=StopReason.MANUAL)
        mock_meta_store.archive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_not_found_actor_missing_is_silent(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = None
        mock_operator.stop.side_effect = ValueError("actor not found")
        await mgr.stop("sb-1")
        mock_meta_store.archive.assert_not_awaited()

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
    async def test_get_status_keeps_cached_phases_when_operator_phases_empty(
        self, mgr, mock_meta_store, mock_operator
    ):
        mock_meta_store.get.return_value = {
            "state": State.PENDING,
            "phases": {"image_pull": {"status": "running", "message": "pulling"}},
        }
        mock_operator.get_status.return_value = {
            "state": State.PENDING,
            "host_name": "h1",
            "host_ip": "1.2.3.4",
            "phases": {},
            "port_mapping": {},
        }

        result = await mgr.get_status("sb-1")

        assert result.status == {"image_pull": {"status": "running", "message": "pulling"}}

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


# ---------------------------------------------------------------------------
# TestManagerRestart
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_config():
    cfg = MagicMock()
    cfg.container_name = "sb-1"
    cfg.auto_clear_time = 30
    return cfg


@pytest.fixture
def mgr_restart(mgr, mock_docker_config):
    mgr.deployment_manager = MagicMock()
    mgr.deployment_manager.init_config = AsyncMock(return_value=mock_docker_config)
    mgr.restart_async = SandboxManager.restart_async.__wrapped__.__get__(mgr)
    return mgr


class TestManagerRestart:
    @pytest.mark.asyncio
    async def test_sandbox_not_found_raises(self, mgr_restart, mock_meta_store):
        mock_meta_store.get.return_value = None
        with pytest.raises(BadRequestRockError, match="not found"):
            await mgr_restart.restart_async(MagicMock())

    @pytest.mark.asyncio
    async def test_non_stopped_state_raises(self, mgr_restart, mock_meta_store):
        mock_meta_store.get.return_value = {"state": State.RUNNING, "host_ip": "1.2.3.4", "host_name": "w1"}
        with pytest.raises(BadRequestRockError):
            await mgr_restart.restart_async(MagicMock())

    @pytest.mark.asyncio
    async def test_stopped_success_returns_response(self, mgr_restart, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {
            "state": State.STOPPED,
            "host_ip": "1.2.3.4",
            "host_name": "worker-1",
            "image": "python:3.11",
            "memory": "2g",
            "cpus": 1,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "auto_clear_time_minutes": 30,
            },
        }
        mock_operator.restart = AsyncMock(return_value={"host_name": "worker-1", "host_ip": "1.2.3.4"})
        result = await mgr_restart.restart_async("sb-1")
        assert isinstance(result, SandboxStartResponse)
        assert result.sandbox_id == "sb-1"
        assert result.host_ip == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_stopped_success_calls_operator_restart(self, mgr_restart, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {
            "state": State.STOPPED,
            "host_ip": "1.2.3.4",
            "host_name": "worker-1",
            "image": "python:3.11",
            "memory": "2g",
            "cpus": 1,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "auto_clear_time_minutes": 30,
            },
        }
        mock_operator.restart = AsyncMock(return_value={"host_name": "worker-1", "host_ip": "1.2.3.4"})
        await mgr_restart.restart_async("sb-1")
        mock_operator.restart.assert_awaited_once()
        called_config = mock_operator.restart.await_args.args[0]
        assert called_config.container_name == "sb-1"
        assert called_config.image == "python:3.11"

    @pytest.mark.asyncio
    async def test_stopped_success_calls_meta_update(self, mgr_restart, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {
            "state": State.STOPPED,
            "host_ip": "1.2.3.4",
            "host_name": "worker-1",
            "image": "python:3.11",
            "memory": "2g",
            "cpus": 1,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "auto_clear_time_minutes": 30,
            },
        }
        mock_operator.restart = AsyncMock(return_value={"host_name": "worker-1", "host_ip": "1.2.3.4"})
        await mgr_restart.restart_async("sb-1")
        mock_meta_store.update.assert_awaited()
        mock_meta_store.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_operator_failure_propagates(self, mgr_restart, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {
            "state": State.STOPPED,
            "host_ip": "1.2.3.4",
            "host_name": "worker-1",
            "image": "python:3.11",
            "memory": "2g",
            "cpus": 1,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "auto_clear_time_minutes": 30,
            },
        }
        mock_operator.restart = AsyncMock(side_effect=BadRequestRockError("docker start failed"))
        with pytest.raises(BadRequestRockError, match="docker start failed"):
            await mgr_restart.restart_async("sb-1")


# ---------------------------------------------------------------------------
# TestManagerStart
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr_start(mgr, mock_meta_store, mock_operator, mock_docker_config):
    mgr.deployment_manager = MagicMock()
    mgr.deployment_manager.init_config = AsyncMock(return_value=mock_docker_config)
    mgr._check_sandbox_exists_in_redis = AsyncMock()
    mgr.validate_sandbox_spec = MagicMock()
    mgr.rock_config = MagicMock()
    mgr.rock_config.runtime.use_standard_spec_only = False

    mgr.start_async = SandboxManager.start_async.__wrapped__.__get__(mgr)
    mgr._build_sandbox_info_metadata = AsyncMock()
    mgr.start = SandboxManager.start.__wrapped__.__get__(mgr)
    mgr.get_status = AsyncMock(return_value=MagicMock(is_alive=True, state=State.RUNNING))
    return mgr


class TestManagerStart:
    @pytest.mark.asyncio
    async def test_start_writes_meta_store_and_waits_running(self, mgr_start, mock_meta_store, mock_operator):
        config = MagicMock()
        config.image = "python:3.11"
        result = await mgr_start.start(config)
        assert isinstance(result, SandboxStartResponse)
        assert result.sandbox_id == "sb-1"
        mock_meta_store.create.assert_awaited_once()
        mgr_start.get_status.assert_awaited_once_with("sb-1")

    @pytest.mark.asyncio
    async def test_start_retries_until_sandbox_running(self, mgr_start):
        call_count = 0

        async def running_after_retries(sandbox_id):
            nonlocal call_count
            call_count += 1
            return MagicMock(is_alive=call_count >= 3, state=State.RUNNING if call_count >= 3 else State.PENDING)

        mgr_start.get_status = running_after_retries
        config = MagicMock()
        config.image = "python:3.11"
        result = await mgr_start.start(config)
        assert isinstance(result, SandboxStartResponse)
        assert call_count >= 3

    @pytest.mark.asyncio
    async def test_start_timeout_raises(self, mgr_start):
        mgr_start.get_status = AsyncMock(return_value=MagicMock(is_alive=False, state=State.PENDING))
        config = MagicMock()
        config.image = "python:3.11"
        with patch("rock.sandbox.sandbox_manager.REQUEST_TIMEOUT_SECONDS", 0):
            with pytest.raises(TimeoutError, match="not running after"):
                await mgr_start.start(config)
