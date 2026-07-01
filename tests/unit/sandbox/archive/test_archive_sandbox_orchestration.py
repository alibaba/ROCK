"""Unit tests for SandboxManager.archive_sandbox and restart_from_archived."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def manager():
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m._meta_store = AsyncMock()
    m._meta_store._db = AsyncMock()
    m._meta_store._db.get = AsyncMock(
        return_value={"sandbox_id": "sbx-1", "spec": {"container_name": "sbx-1", "image": "img:latest"}}
    )
    m._operator = MagicMock()
    m._operator.supports_archive.return_value = True
    m._operator.start_archive = AsyncMock()
    m._operator.start_restore = AsyncMock()
    m._dir_storage = AsyncMock()
    m._dir_storage.client_config = {"endpoint": "e", "bucket": "b", "access_key": "a", "secret_key": "s", "region": "r"}
    m._dir_storage.delete = AsyncMock(return_value=True)
    m._image_storage = AsyncMock()
    m._image_storage.registry_url = "localhost:5000"
    m._image_storage.client_config = {"registry_url": "localhost:5000"}
    m._image_storage.delete = AsyncMock(return_value=True)

    from rock.config import ArchiveConfig

    m.rock_config = MagicMock()
    m.rock_config.lifecycle.archive = ArchiveConfig()

    m.archive_sandbox = SandboxManager.archive_sandbox.__get__(m, SandboxManager)
    m.restart_from_archived = SandboxManager.restart_from_archived.__get__(m, SandboxManager)
    return m


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


class TestArchiveSandbox:
    async def test_happy_path(self, manager, sm_stopped):
        manager._get_current_statemachine = AsyncMock(return_value=sm_stopped)
        await manager.archive_sandbox("sbx-1")

        sm_stopped.send.assert_called_once()
        assert sm_stopped.send.call_args[0][0] == "archive"
        kwargs = sm_stopped.send.call_args[1]
        assert kwargs["operator"] is manager._operator
        assert kwargs["dir_storage"] is manager._dir_storage
        assert kwargs["image_storage"] is manager._image_storage
        assert "archive_params" in kwargs

    async def test_no_operator_raises(self, manager, sm_stopped):
        manager._operator = None
        with pytest.raises(BadRequestRockError):
            await manager.archive_sandbox("sbx-1")

    async def test_not_found_raises(self, manager):
        manager._get_current_statemachine = AsyncMock(return_value=None)
        with pytest.raises(BadRequestRockError):
            await manager.archive_sandbox("sbx-1")

    async def test_passes_storage(self, manager, sm_stopped):
        """Verify storage is passed through to on_archive for operator use."""
        manager._get_current_statemachine = AsyncMock(return_value=sm_stopped)

        await manager.archive_sandbox("sbx-1")

        sm_stopped.send.assert_called_once()
        kwargs = sm_stopped.send.call_args[1]
        assert kwargs["dir_storage"] is manager._dir_storage
        assert kwargs["image_storage"] is manager._image_storage


class TestRestartFromArchived:
    async def test_happy_path(self, manager, sm_archived):
        manager._get_current_statemachine = AsyncMock(return_value=sm_archived)
        await manager.restart_from_archived("sbx-1")

        sm_archived.send.assert_called_once()
        assert sm_archived.send.call_args[0][0] == "restore"
        kwargs = sm_archived.send.call_args[1]
        assert kwargs["operator"] is manager._operator
        assert kwargs["dir_storage"] is manager._dir_storage
        assert kwargs["image_storage"] is manager._image_storage

    async def test_not_archived_skips(self, manager, sm_stopped):
        manager._get_current_statemachine = AsyncMock(return_value=sm_stopped)
        await manager.restart_from_archived("sbx-1")

        manager._operator.start_restore.assert_not_called()
        sm_stopped.send.assert_not_called()

    async def test_not_found_raises(self, manager):
        manager._get_current_statemachine = AsyncMock(return_value=None)
        with pytest.raises(BadRequestRockError):
            await manager.restart_from_archived("sbx-1")

    async def test_no_spec_raises(self, manager, sm_archived):
        sm_archived.sandbox_info = {
            "sandbox_id": "sbx-1",
            "state": State.ARCHIVED,
            "archive_time": "2026-01-01T000000Z",
            "host_ip": "10.0.0.1",
            "spec": {},
        }
        manager._get_current_statemachine = AsyncMock(return_value=sm_archived)
        with pytest.raises(BadRequestRockError):
            await manager.restart_from_archived("sbx-1")
