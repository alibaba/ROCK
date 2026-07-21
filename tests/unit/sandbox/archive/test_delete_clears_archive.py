"""Unit tests for delete() passing dir_storage/image_storage to on_delete for archive cleanup."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m._operator = MagicMock()
    m._operator.supports_running_delete = False
    m._dir_storage = AsyncMock()
    m._dir_storage.client_config = {"endpoint": "e", "bucket": "b", "access_key": "a", "secret_key": "s", "region": "r"}
    m._dir_storage.delete = AsyncMock(return_value=True)
    m._image_storage = AsyncMock()
    m._image_storage.registry_url = "localhost:5000"
    m._image_storage.client_config = {"registry_url": "localhost:5000"}
    m._image_storage.delete = AsyncMock(return_value=True)

    m.delete = SandboxManager.delete.__get__(m, SandboxManager)
    return m


@pytest.fixture
def sm_archived():
    sm = AsyncMock()
    sm.current_state.value = State.ARCHIVED
    sm.sandbox_info = {
        "sandbox_id": "sbx-1",
        "state": State.ARCHIVED,
        "archive_time": "2026-01-01T000000Z",
        "host_ip": "10.0.0.1",
        "spec": {"container_name": "sbx-1", "image": "img:latest"},
    }
    return sm


@pytest.fixture
def sm_stopped():
    sm = AsyncMock()
    sm.current_state.value = State.STOPPED
    sm.sandbox_info = {
        "sandbox_id": "sbx-1",
        "state": State.STOPPED,
        "host_ip": "10.0.0.1",
        "spec": {"container_name": "sbx-1", "image": "img:latest"},
    }
    return sm


class TestDeletePassesStorageToCallback:
    async def test_delete_archived_passes_storage(self, manager, sm_archived):
        manager._get_current_statemachine = AsyncMock(return_value=sm_archived)
        await manager.delete("sbx-1")

        sm_archived.send.assert_called_once()
        assert sm_archived.send.call_args[0][0] == "delete"
        kwargs = sm_archived.send.call_args[1]
        assert kwargs["dir_storage"] is manager._dir_storage
        assert kwargs["image_storage"] is manager._image_storage

    async def test_delete_stopped_passes_storage(self, manager, sm_stopped):
        manager._get_current_statemachine = AsyncMock(return_value=sm_stopped)
        await manager.delete("sbx-1")

        sm_stopped.send.assert_called_once()
        kwargs = sm_stopped.send.call_args[1]
        assert kwargs["dir_storage"] is manager._dir_storage
        assert kwargs["image_storage"] is manager._image_storage

    async def test_delete_without_storage_passes_none(self, manager, sm_stopped):
        manager._dir_storage = None
        manager._image_storage = None
        manager._get_current_statemachine = AsyncMock(return_value=sm_stopped)
        await manager.delete("sbx-1")

        sm_stopped.send.assert_called_once()
        kwargs = sm_stopped.send.call_args[1]
        assert kwargs["dir_storage"] is None
        assert kwargs["image_storage"] is None

    async def test_delete_running_still_rejected(self, manager):
        sm = AsyncMock()
        sm.current_state.value = State.RUNNING
        sm.sandbox_info = {"sandbox_id": "sbx-1"}
        manager._get_current_statemachine = AsyncMock(return_value=sm)

        with pytest.raises(BadRequestRockError, match="stopped or archived"):
            await manager.delete("sbx-1")

    async def test_delete_already_deleted_noop(self, manager):
        sm = AsyncMock()
        sm.current_state.value = State.DELETED
        manager._get_current_statemachine = AsyncMock(return_value=sm)

        await manager.delete("sbx-1")

        sm.send.assert_not_called()
