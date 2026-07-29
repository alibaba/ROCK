"""Unit tests for SandboxManager._auto_delete_stopped."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.common.constants import DeleteReason


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m.delete = AsyncMock()
    m._delete_expired_candidates = SandboxManager._delete_expired_candidates.__get__(m, SandboxManager)
    m._auto_delete_stopped = SandboxManager._auto_delete_stopped.__get__(m, SandboxManager)
    return m


class TestAutoDeleteStopped:
    async def test_zero_deletes_stopped_sandbox_immediately(self, manager):
        now = datetime.now(timezone.utc)
        due = now.isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_skips_sandbox_within_threshold(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(return_value=[])
        await manager._auto_delete_stopped()
        manager._meta_store.list_expired_by.assert_awaited_once()
        manager.delete.assert_not_called()

    async def test_deletes_stopped_sandbox_past_threshold(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_lists_only_stopped_sandboxes(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(return_value=[])
        await manager._auto_delete_stopped()
        manager._meta_store.list_expired_by.assert_awaited_once()
        args = manager._meta_store.list_expired_by.await_args.args
        assert args[0] == "stopped"
        assert args[1] == "deleted"

    async def test_uses_auto_transition_time_when_present(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_skips_future_auto_delete_time(self, manager):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(seconds=3600)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": future, "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_delete_failure_does_not_propagate(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": due, "state": "stopped"}]
        )
        manager.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
        await manager._auto_delete_stopped()

    async def test_deletes_with_bounded_concurrency(self, manager, monkeypatch):
        from rock.sandbox import sandbox_manager as sandbox_manager_module

        monkeypatch.setattr(sandbox_manager_module, "_AUTO_DELETE_CONCURRENCY", 2)
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[
                {"sandbox_id": f"sbx-{index}", "auto_transition_time": due, "state": "stopped"} for index in range(5)
            ]
        )
        active = 0
        peak = 0

        async def delete_sandbox(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        manager.delete = AsyncMock(side_effect=delete_sandbox)

        await manager._auto_delete_stopped()

        assert manager.delete.await_count == 5
        assert peak == 2

    async def test_one_delete_failure_does_not_cancel_other_candidates(self, manager):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[
                {"sandbox_id": f"sbx-{index}", "auto_transition_time": due, "state": "stopped"} for index in range(3)
            ]
        )

        async def delete_sandbox(sandbox_id, **kwargs):
            if sandbox_id == "sbx-1":
                raise RuntimeError("delete failed")

        manager.delete = AsyncMock(side_effect=delete_sandbox)

        await manager._auto_delete_stopped()

        assert manager.delete.await_count == 3

    async def test_empty_list(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(return_value=[])
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_missing_auto_transition_time_is_skipped(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[{"sandbox_id": "sbx-1", "auto_transition_time": "", "state": "stopped"}]
        )
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()

    async def test_list_expired_by_failure(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(side_effect=RuntimeError("db error"))
        await manager._auto_delete_stopped()
        manager.delete.assert_not_called()
