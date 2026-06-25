"""Unit tests for SandboxManager._reconcile_archiving scanner."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.config import SandboxLifecycleConfig


@pytest.fixture
def manager():
    """Create a minimal SandboxManager-like object with mocked deps."""
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m.rock_config.lifecycle = SandboxLifecycleConfig()
    m._meta_store = AsyncMock()
    m._operator = MagicMock()
    m._operator.supports_archive.return_value = True
    m._dir_storage = AsyncMock()
    m._dir_storage.client_config = {
        "endpoint": "http://localhost:9000",
        "bucket": "b",
        "access_key": "a",
        "secret_key": "s",
        "region": "r",
    }
    m._image_storage = AsyncMock()
    m._image_storage.registry_url = "localhost:5000"
    m._image_storage.client_config = {"registry_url": "localhost:5000"}
    m._get_current_statemachine = AsyncMock()
    m._reconcile_archiving = SandboxManager._reconcile_archiving.__get__(m, SandboxManager)
    return m


class TestCheckArchiveProgress:
    async def test_empty_list_does_nothing(self, manager):
        manager._meta_store.list_by = AsyncMock(return_value=[])
        await manager._reconcile_archiving()
        manager._image_storage.exists.assert_not_called()

    async def test_image_exists_triggers_archive_done(self, manager):
        info = {
            "sandbox_id": "sbx-1",
            "archive_time": "2026-01-01T000000Z",
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
        manager._image_storage.exists = AsyncMock(return_value=True)

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once()
        call_kwargs = sm_mock.send.call_args
        assert call_kwargs[0][0] == "archive_done"

    async def test_image_not_exist_within_timeout_skips(self, manager):
        now = datetime.now(timezone.utc).isoformat()
        info = {
            "sandbox_id": "sbx-1",
            "archive_time": "2026-01-01T000000Z",
            "state_history": [{"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": now}],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._image_storage.exists = AsyncMock(return_value=False)

        await manager._reconcile_archiving()

        manager._get_current_statemachine.assert_not_called()

    async def test_timeout_triggers_archive_failed(self, manager):
        old_time = "2020-01-01T00:00:00+00:00"
        info = {
            "sandbox_id": "sbx-1",
            "archive_time": "t1",
            "state_history": [
                {"from_state": "stopped", "to_state": "archiving", "event": "archive", "timestamp": old_time}
            ],
            "state": State.ARCHIVING,
        }
        manager._meta_store.list_by = AsyncMock(return_value=[info])
        manager._image_storage.exists = AsyncMock(return_value=False)

        sm_mock = AsyncMock()
        sm_mock.current_state.value = State.ARCHIVING
        manager._get_current_statemachine = AsyncMock(return_value=sm_mock)

        await manager._reconcile_archiving()

        sm_mock.send.assert_called_once()
        assert sm_mock.send.call_args[0][0] == "archive_failed"

    async def test_operator_not_supporting_archive_returns_early(self, manager):
        manager._operator.supports_archive.return_value = False
        manager._meta_store.list_by = AsyncMock()
        await manager._reconcile_archiving()
        manager._meta_store.list_by.assert_not_called()
