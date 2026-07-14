"""Tests for SandboxManager lifecycle methods.

Verifies that stop(), get_status(), and start_async() behave correctly for
each sandbox state, using lightweight mocks (no Ray / Docker).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStartResponse
from rock.common.constants import StopReason
from rock.config import AutoTransitionConfig, SandboxLifecycleConfig
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
    async def test_running_get_status_filters_inactive_auto_transition_times(self, mgr, mock_meta_store, mock_operator):
        mock_meta_store.get.return_value = {
            "state": State.RUNNING,
            "auto_archive_seconds": 600,
            "auto_delete_seconds": 1200,
            "auto_stop_time": "2026-01-01T00:30:00+00:00",
            "auto_transition_state": State.ARCHIVED,
            "auto_transition_time": "2026-01-01T00:10:00+00:00",
        }
        mock_operator.get_status.return_value = {
            "state": State.RUNNING,
            "host_name": "h1",
            "host_ip": "1.2.3.4",
            "phases": {},
            "port_mapping": {},
        }

        result = await mgr.get_status("sb-1")

        assert result.auto_stop_time == "2026-01-01T00:30:00+00:00"
        assert result.auto_archive_time is None
        assert result.auto_delete_time is None

    @pytest.mark.asyncio
    async def test_get_status_keeps_cached_phases_when_operator_phases_empty(self, mgr, mock_meta_store, mock_operator):
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
    cfg.image = "python:3.11"
    cfg.cpus = 1.0
    cfg.memory = "1g"
    cfg.disk = None
    cfg.auto_clear_time = 30
    cfg.auto_archive_seconds = None
    cfg.auto_delete_seconds = None
    cfg.remove_container = True
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
    mgr.rock_config.lifecycle = SandboxLifecycleConfig(
        auto_transition=AutoTransitionConfig(auto_delete_seconds=3600, auto_delete_archived_seconds=7200)
    )

    mgr.start_async = SandboxManager.start_async.__wrapped__.__get__(mgr)
    mgr._apply_user_auto_transition_policy = SandboxManager._apply_user_auto_transition_policy.__get__(mgr)

    async def build_metadata(sandbox_info, user_info, cluster_info):
        sandbox_info["state"] = State.PENDING
        sandbox_info.setdefault("create_time", "2026-07-20T12:00:00+08:00")

    mgr._build_sandbox_info_metadata = AsyncMock(side_effect=build_metadata)

    mgr.created_spec = {}

    async def capture_create(sandbox_id, sandbox_info, timeout_info=None, deployment_config=None):
        mgr.created_spec.update(
            auto_archive_seconds=deployment_config.auto_archive_seconds,
            auto_delete_seconds=deployment_config.auto_delete_seconds,
        )

    mock_meta_store.create.side_effect = capture_create
    mgr.start = SandboxManager.start.__wrapped__.__get__(mgr)
    mgr.get_status = AsyncMock(return_value=MagicMock(is_alive=True, state=State.RUNNING))
    return mgr


class TestManagerStart:
    @pytest.mark.asyncio
    async def test_start_registers_effective_policy_before_operator_submit(
        self, mgr_start, mock_meta_store, mock_operator
    ):
        async def assert_pending_exists(*args, **kwargs):
            mock_meta_store.create.assert_awaited_once()
            mock_meta_store.update.assert_awaited_once()
            pending_info = mock_meta_store.update.await_args.args[1]
            assert pending_info["sandbox_id"] == "sb-1"
            assert pending_info["state"] == State.PENDING
            assert pending_info["auto_delete_seconds"] == 3600
            return {"host_name": "h1", "host_ip": "1.2.3.4", "memory": "1g", "cpus": 1.0}

        mock_operator.submit.side_effect = assert_pending_exists

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_meta_store.update.await_count == 2

    @pytest.mark.asyncio
    async def test_submit_failure_keeps_pending_record_with_spec_timeout_and_effective_policy(
        self, mgr_start, mock_meta_store, mock_operator, mock_docker_config
    ):
        mock_docker_config.auto_delete_seconds = 7200
        mock_operator.submit.side_effect = RuntimeError("ray submit failed")

        with pytest.raises(RuntimeError, match="ray submit failed"):
            await mgr_start.start_async(MagicMock(image="python:3.11"))

        mock_meta_store.create.assert_awaited_once()
        assert mock_meta_store.create.await_args.kwargs["timeout_info"]
        assert mgr_start.created_spec["auto_delete_seconds"] == 7200
        mock_meta_store.update.assert_awaited_once()
        pending_info = mock_meta_store.update.await_args.args[1]
        assert pending_info["state"] == State.PENDING
        assert pending_info["auto_delete_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_uses_cluster_auto_delete_when_user_policy_is_unspecified(
        self, mgr_start, mock_docker_config, mock_meta_store
    ):
        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds == 3600
        assert mock_docker_config.remove_container is False
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_delete_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_cluster_zero_defaults_to_immediate_delete(self, mgr_start, mock_docker_config, mock_meta_store):
        mgr_start.rock_config.lifecycle.auto_transition.auto_delete_seconds = 0

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds == 0
        assert mock_docker_config.remove_container is True
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_delete_seconds"] == 0

    @pytest.mark.asyncio
    async def test_no_cluster_default_keeps_auto_delete_disabled(self, mgr_start, mock_docker_config, mock_meta_store):
        mgr_start.rock_config.lifecycle.auto_transition.auto_delete_seconds = None

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds is None
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert "auto_delete_seconds" not in sandbox_info

    @pytest.mark.asyncio
    async def test_auto_archive_ignores_user_auto_delete(self, mgr_start, mock_docker_config, mock_meta_store):
        mock_docker_config.auto_archive_seconds = 600
        mock_docker_config.auto_delete_seconds = 10

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds is None
        assert mock_docker_config.remove_container is False
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_archive_seconds"] == 600
        assert "auto_delete_seconds" not in sandbox_info

    @pytest.mark.asyncio
    async def test_auto_archive_is_capped_and_requested_value_is_kept_in_spec(
        self, mgr_start, mock_docker_config, mock_meta_store
    ):
        mock_docker_config.auto_archive_seconds = 3601

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_archive_seconds == 3600
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_archive_seconds"] == 3600
        assert mgr_start.created_spec["auto_archive_seconds"] == 3601

    @pytest.mark.asyncio
    async def test_auto_delete_is_capped_and_requested_value_is_kept_in_spec(
        self, mgr_start, mock_docker_config, mock_meta_store
    ):
        mock_docker_config.auto_delete_seconds = 3601

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds == 3600
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_delete_seconds"] == 3600
        assert mgr_start.created_spec["auto_delete_seconds"] == 3601

    @pytest.mark.asyncio
    async def test_ignored_auto_delete_is_not_validated(self, mgr_start, mock_docker_config, mock_meta_store):
        mock_docker_config.auto_archive_seconds = 600
        mock_docker_config.auto_delete_seconds = 3601

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mock_docker_config.auto_delete_seconds is None
        sandbox_info = mock_meta_store.create.await_args.args[1]
        assert sandbox_info["auto_archive_seconds"] == 600
        assert "auto_delete_seconds" not in sandbox_info
        assert mgr_start.created_spec["auto_archive_seconds"] == 600
        assert mgr_start.created_spec["auto_delete_seconds"] == 3601

    @pytest.mark.asyncio
    async def test_auto_archive_is_unlimited_without_cluster_stopped_retention(
        self, mgr_start, mock_docker_config, mock_meta_store
    ):
        mock_docker_config.auto_archive_seconds = 600
        mgr_start.rock_config.lifecycle.auto_transition.auto_delete_seconds = None

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mgr_start.created_spec["auto_archive_seconds"] == 600
        assert mock_docker_config.auto_archive_seconds == 600
        sandbox_info = mock_meta_store.update.await_args_list[0].args[1]
        assert sandbox_info["auto_archive_seconds"] == 600

    @pytest.mark.asyncio
    async def test_auto_delete_is_unlimited_without_cluster_stopped_retention(
        self, mgr_start, mock_docker_config, mock_meta_store
    ):
        mock_docker_config.auto_delete_seconds = 7200
        mgr_start.rock_config.lifecycle.auto_transition.auto_delete_seconds = None

        await mgr_start.start_async(MagicMock(image="python:3.11"))

        assert mgr_start.created_spec["auto_delete_seconds"] == 7200
        assert mock_docker_config.auto_delete_seconds == 7200
        sandbox_info = mock_meta_store.update.await_args_list[0].args[1]
        assert sandbox_info["auto_delete_seconds"] == 7200

    @pytest.mark.asyncio
    async def test_start_writes_meta_store_and_waits_running(self, mgr_start, mock_meta_store, mock_operator):
        mock_operator.submit.return_value["disk"] = "20g"
        config = MagicMock()
        config.image = "python:3.11"
        result = await mgr_start.start(config)
        assert isinstance(result, SandboxStartResponse)
        assert result.sandbox_id == "sb-1"
        assert result.disk == "20g"
        assert result.disk_limit_rootfs == "20g"
        mock_meta_store.create.assert_awaited_once()
        assert mock_meta_store.update.await_count == 2
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
