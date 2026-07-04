"""Unit tests for SandboxManager._auto_delete_stopped."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.common.constants import DeleteReason
from rock.config import AutoTransitionConfig, SandboxLifecycleConfig


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m.rock_config = MagicMock()
    m.rock_config.lifecycle = SandboxLifecycleConfig(auto_transition=AutoTransitionConfig(auto_delete_seconds=3600))
    m.delete = AsyncMock()
    m._auto_delete_stopped = SandboxManager._auto_delete_stopped.__get__(m, SandboxManager)
    return m


class TestAutoDeleteStopped:
    async def test_disabled_when_zero(self, manager):
        manager.rock_config.lifecycle.auto_transition.auto_delete_seconds = 0
        await manager._auto_delete_stopped()
        manager._meta_store.list_by_in.assert_not_called()

    async def test_skips_sandbox_within_threshold(self, manager):
        now = datetime.now(timezone.utc)
        recent_stop = (now - timedelta(seconds=60)).isoformat()
        manager._meta_store.list_by_in = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "stop_time": recent_stop, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_deletes_stopped_sandbox_past_threshold(self, manager):
        now = datetime.now(timezone.utc)
        old_stop = (now - timedelta(seconds=7200)).isoformat()
        manager._meta_store.list_by_in = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "stop_time": old_stop, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_deletes_archived_sandbox_past_threshold(self, manager):
        now = datetime.now(timezone.utc)
        old_stop = (now - timedelta(seconds=7200)).isoformat()
        manager._meta_store.list_by_in = AsyncMock(
            return_value=[{"sandbox_id": "sbx-2", "stop_time": old_stop, "state": "archived"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_awaited_once_with("sbx-2", reason=DeleteReason.EXPIRED)

    async def test_delete_failure_does_not_propagate(self, manager):
        now = datetime.now(timezone.utc)
        old_stop = (now - timedelta(seconds=7200)).isoformat()
        manager._meta_store.list_by_in = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "stop_time": old_stop, "state": "stopped"}]
        )
        manager.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
        await manager._auto_delete_stopped()

    async def test_empty_list(self, manager):
        manager._meta_store.list_by_in = AsyncMock(return_value=[])
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_missing_stop_time_is_skipped(self, manager):
        manager._meta_store.list_by_in = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "stop_time": "", "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_list_by_in_failure(self, manager):
        manager._meta_store.list_by_in = AsyncMock(side_effect=RuntimeError("db error"))
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()
