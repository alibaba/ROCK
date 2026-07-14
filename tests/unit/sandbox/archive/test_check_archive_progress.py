"""Unit tests for SandboxManager._reconcile_archiving scanner (phase-based detection)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.config import SandboxLifecycleConfig
from rock.deployments.constants import Status
from rock.deployments.status import PhaseStatus, ServiceStatus


@pytest.fixture
def manager():
    """Create a minimal SandboxManager-like object with mocked deps."""
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m.rock_config.lifecycle = SandboxLifecycleConfig()
    m._meta_store = AsyncMock()
    m._operator = AsyncMock()
    m._dir_storage = AsyncMock()
    m._image_storage = AsyncMock()
    m._get_current_statemachine = AsyncMock()
    m._try_advance_archiving = SandboxManager._try_advance_archiving.__get__(m, SandboxManager)
    m._reconcile_archiving = SandboxManager._reconcile_archiving.__get__(m, SandboxManager)
    return m


def _make_remote_status(image_status=None, log_status=None):
    """Build a ServiceStatus with archive phases."""
    phases = {}
    if image_status:
        phases["image_archive"] = PhaseStatus(status=image_status, message="test")
    if log_status:
        phases["log_archive"] = PhaseStatus(status=log_status, message="test")
    if phases:
        return ServiceStatus(phases=phases)
    return ServiceStatus()


class TestCheckArchiveProgress:
    async def test_empty_list_does_nothing(self, manager):
        manager._meta_store.list_by = AsyncMock(return_value=[])
        await manager._reconcile_archiving()
        manager._operator.get_remote_status.assert_not_called()

    async def test_image_success_triggers_archive_done(self, manager):
        info = {
            "sandbox_id": "sbx-1",
            "host_ip": "10.0.0.1",
            "state_history": [
                {
                    "from_state": "stopped",
                    "to_state": "archiving",
                    "event": "archive",
                    "timestamp": "2026-01-01T000000Z",
                }
            ],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._operator.get_remote_status = AsyncMock(
            return_value=_make_remote_status(image_status=Status.SUCCESS, log_status=Status.SUCCESS)
        )

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info

        async def _send_side_effect(*args, **kwargs):
            sm_mock.current_state.value = State.ARCHIVED

        sm_mock.send.side_effect = _send_side_effect
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once()
        assert sm_mock.send.call_args[0][0] == "archive_done"

    async def test_image_success_without_log_still_triggers_archive_done(self, manager):
        """image_archive success alone is sufficient (log_archive is optional)."""
        now = datetime.now(timezone.utc).isoformat()
        info = {
            "sandbox_id": "sbx-1",
            "host_ip": "10.0.0.1",
            "state_history": [{"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": now}],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._operator.get_remote_status = AsyncMock(return_value=_make_remote_status(image_status=Status.SUCCESS))

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info

        async def _send_side_effect(*args, **kwargs):
            sm_mock.current_state.value = State.ARCHIVED

        sm_mock.send.side_effect = _send_side_effect
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once_with(
            "archive_done",
            sandbox_id="sbx-1",
            meta_store=manager._meta_store,
            auto_transition=manager.rock_config.lifecycle.auto_transition,
        )

    async def test_image_running_within_timeout_skips(self, manager):
        now = datetime.now(timezone.utc).isoformat()
        info = {
            "sandbox_id": "sbx-1",
            "host_ip": "10.0.0.1",
            "state_history": [{"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": now}],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._operator.get_remote_status = AsyncMock(return_value=_make_remote_status(image_status=Status.RUNNING))

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_not_called()

    async def test_image_failed_triggers_archive_failed(self, manager):
        now = datetime.now(timezone.utc).isoformat()
        info = {
            "sandbox_id": "sbx-1",
            "host_ip": "10.0.0.1",
            "state_history": [{"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": now}],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._operator.get_remote_status = AsyncMock(return_value=_make_remote_status(image_status=Status.FAILED))

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once()
        assert sm_mock.send.call_args[0][0] == "archive_failed"

    async def test_timeout_fallback_triggers_archive_failed(self, manager):
        """If phases are empty (actor crashed) and timeout elapsed, fall back to archive_failed."""
        old_time = "2020-01-01T00:00:00+00:00"
        info = {
            "sandbox_id": "sbx-1",
            "host_ip": "10.0.0.1",
            "state_history": [
                {"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": old_time}
            ],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        # Empty phases (actor crashed before writing any status)
        manager._operator.get_remote_status = AsyncMock(return_value=ServiceStatus())

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once()
        assert sm_mock.send.call_args[0][0] == "archive_failed"

    async def test_no_host_ip_skips(self, manager):
        """If sandbox_info has no host_ip, _try_advance_archiving skips."""
        now = datetime.now(timezone.utc).isoformat()
        info = {
            "sandbox_id": "sbx-1",
            "state_history": [{"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": now}],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        sm_mock.sandbox_info = info
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_not_called()
        manager._operator.get_remote_status.assert_not_called()
