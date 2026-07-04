"""Tests: archive methods must return errors / skip gracefully when archive is not configured."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.config import SandboxLifecycleConfig
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def manager_no_archive():
    """SandboxManager-like mock WITHOUT _dir_storage / _image_storage."""
    from rock.sandbox.sandbox_manager import SandboxManager

    m = MagicMock(spec=SandboxManager)
    m.rock_config.lifecycle = SandboxLifecycleConfig()
    m._meta_store = AsyncMock()
    m._operator = MagicMock()

    m._dir_storage = None
    m._image_storage = None

    m.archive_sandbox = SandboxManager.archive_sandbox.__get__(m, SandboxManager)
    m.restart_async = SandboxManager.restart_async.__get__(m, SandboxManager)
    m._reconcile_archiving = SandboxManager._reconcile_archiving.__get__(m, SandboxManager)
    m._try_advance_archiving = SandboxManager._try_advance_archiving.__get__(m, SandboxManager)
    m._get_current_statemachine = AsyncMock()
    m._auto_archive_stopped = SandboxManager._auto_archive_stopped.__get__(m, SandboxManager)
    return m


class TestArchiveNotConfigured:
    async def test_archive_sandbox_raises_error(self, manager_no_archive):
        with pytest.raises(BadRequestRockError, match="archive not configured"):
            await manager_no_archive.archive_sandbox("sbx-1")

    async def test_restart_async_archived_raises_error(self, manager_no_archive):
        from rock.actions.sandbox.response import State

        sm = AsyncMock()
        sm.current_state.value = State.ARCHIVED
        manager_no_archive._get_current_statemachine.return_value = sm
        with pytest.raises(BadRequestRockError, match="archive not configured"):
            await manager_no_archive.restart_async("sbx-1")

    async def test_reconcile_archiving_empty_list(self, manager_no_archive):
        manager_no_archive._meta_store.list_by = AsyncMock(return_value=[])
        await manager_no_archive._reconcile_archiving()
        manager_no_archive._meta_store.list_by.assert_called_once()

    async def test_auto_archive_stopped_skips(self, manager_no_archive):
        manager_no_archive.rock_config.lifecycle.auto_transition.auto_archive_seconds = 3600
        await manager_no_archive._auto_archive_stopped()
        manager_no_archive._meta_store.list_by.assert_not_called()


class TestArchiveOperatorNotConfigured:
    async def test_archive_sandbox_raises_error(self, manager_no_archive):
        manager_no_archive._dir_storage = AsyncMock()
        manager_no_archive._image_storage = AsyncMock()
        manager_no_archive._operator = None
        with pytest.raises(BadRequestRockError, match="archive not supported"):
            await manager_no_archive.archive_sandbox("sbx-1")

    async def test_reconcile_archiving_empty_list(self, manager_no_archive):
        manager_no_archive._meta_store.list_by = AsyncMock(return_value=[])
        await manager_no_archive._reconcile_archiving()
        manager_no_archive._meta_store.list_by.assert_called_once()

    async def test_auto_archive_stopped_skips(self, manager_no_archive):
        manager_no_archive._dir_storage = None
        manager_no_archive._image_storage = None
        manager_no_archive.rock_config.lifecycle.auto_transition.auto_archive_seconds = 3600
        await manager_no_archive._auto_archive_stopped()
        manager_no_archive._meta_store.list_by.assert_not_called()
