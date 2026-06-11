"""Unit tests for SandboxManager._auto_delete_stopped."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.common.constants import DeleteReason
from rock.config import SandboxLifecycleConfig


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m.rock_config = MagicMock()
    m.rock_config.lifecycle = SandboxLifecycleConfig(auto_delete_after_sec=3600)
    m.delete = AsyncMock()
    m._auto_delete_stopped = SandboxManager._auto_delete_stopped.__get__(m, SandboxManager)
    return m


class TestAutoDeleteStopped:
    async def test_disabled_when_zero(self, manager):
        manager.rock_config.lifecycle.auto_delete_after_sec = 0
        result = await manager._auto_delete_stopped()
        assert result == set()
        manager._meta_store.list_by.assert_not_called()

    async def test_skips_sandbox_within_threshold(self, manager):
        now = datetime.now(timezone.utc)
        recent_stop = (now - timedelta(seconds=60)).isoformat()
        manager._meta_store.list_by = AsyncMock(return_value=[{"sandbox_id": "sbx-1", "stop_time": recent_stop}])
        result = await manager._auto_delete_stopped()
        assert result == set()
        manager.delete.assert_not_called()

    async def test_deletes_sandbox_past_threshold(self, manager):
        now = datetime.now(timezone.utc)
        old_stop = (now - timedelta(seconds=7200)).isoformat()
        manager._meta_store.list_by = AsyncMock(return_value=[{"sandbox_id": "sbx-1", "stop_time": old_stop}])
        result = await manager._auto_delete_stopped()
        assert "sbx-1" in result
        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_delete_failure_does_not_propagate(self, manager):
        now = datetime.now(timezone.utc)
        old_stop = (now - timedelta(seconds=7200)).isoformat()
        manager._meta_store.list_by = AsyncMock(return_value=[{"sandbox_id": "sbx-1", "stop_time": old_stop}])
        manager.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
        result = await manager._auto_delete_stopped()
        assert result == set()

    async def test_empty_list_returns_empty(self, manager):
        manager._meta_store.list_by = AsyncMock(return_value=[])
        result = await manager._auto_delete_stopped()
        assert result == set()

    async def test_missing_stop_time_is_skipped(self, manager):
        manager._meta_store.list_by = AsyncMock(return_value=[{"sandbox_id": "sbx-1", "stop_time": ""}])
        result = await manager._auto_delete_stopped()
        assert result == set()
        manager.delete.assert_not_called()

    async def test_list_by_failure_returns_empty(self, manager):
        manager._meta_store.list_by = AsyncMock(side_effect=RuntimeError("db error"))
        result = await manager._auto_delete_stopped()
        assert result == set()
