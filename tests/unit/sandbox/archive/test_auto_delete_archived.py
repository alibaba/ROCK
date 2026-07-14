"""Unit tests for SandboxManager._auto_delete_archived."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.common.constants import DeleteReason


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    instance = MagicMock(spec=SandboxManager)
    instance._meta_store = AsyncMock()
    instance.delete = AsyncMock()
    instance._auto_delete_archived = SandboxManager._auto_delete_archived.__get__(instance, SandboxManager)
    return instance


class TestAutoDeleteArchived:
    async def test_queries_only_archived_by_delete_deadline(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(return_value=[])

        await manager._auto_delete_archived()

        args = manager._meta_store.list_expired_by.await_args.args
        assert args[0] == "archived"
        assert args[1] == "deleted"

    async def test_deletes_archived_sandbox_past_deadline(self, manager):
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[
                {
                    "sandbox_id": "sbx-1",
                    "auto_transition_time": due,
                    "state": "archived",
                }
            ]
        )

        await manager._auto_delete_archived()

        manager.delete.assert_awaited_once_with("sbx-1", reason=DeleteReason.EXPIRED)

    async def test_skips_future_deadline(self, manager):
        future = (datetime.now(timezone.utc) + timedelta(seconds=3600)).isoformat()
        manager._meta_store.list_expired_by = AsyncMock(
            return_value=[
                {
                    "sandbox_id": "sbx-1",
                    "auto_transition_time": future,
                    "state": "archived",
                }
            ]
        )

        await manager._auto_delete_archived()

        manager.delete.assert_not_called()

    async def test_list_failure_does_not_propagate(self, manager):
        manager._meta_store.list_expired_by = AsyncMock(side_effect=RuntimeError("db error"))

        await manager._auto_delete_archived()

        manager.delete.assert_not_called()
