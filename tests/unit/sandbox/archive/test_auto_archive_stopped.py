"""Unit tests for SandboxManager._auto_archive_stopped."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m._operator = MagicMock()
    m._dir_storage = MagicMock()
    m._image_storage = MagicMock()
    m.archive_sandbox = AsyncMock()
    m._auto_archive_stopped = SandboxManager._auto_archive_stopped.__get__(m, SandboxManager)
    return m


class TestAutoArchiveStopped:
    async def test_zero_archives_stopped_sandbox_immediately(self, manager):
        now = datetime.now(timezone.utc)
        due = now.isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_archive_stopped()
        manager.archive_sandbox.assert_awaited_once_with("sbx-1")

    async def test_skips_sandbox_within_threshold(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(return_value=[])
        await manager._auto_archive_stopped()
        manager.archive_sandbox.assert_not_called()

    async def test_archives_sandbox_past_threshold(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_archive_stopped()
        manager.archive_sandbox.assert_awaited_once_with("sbx-1")

    async def test_uses_auto_transition_time_when_present(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_archive_stopped()
        manager.archive_sandbox.assert_awaited_once_with("sbx-1")

    async def test_skips_future_auto_archive_time(self, manager):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(seconds=3600)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": future, "state": "stopped"}]
        )
        await manager._auto_archive_stopped()
        manager.archive_sandbox.assert_not_called()
