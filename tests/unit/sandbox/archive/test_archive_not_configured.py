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
    m._operator.supports_archive.return_value = True

    m._dir_storage = None
    m._image_storage = None

    m.archive_sandbox = SandboxManager.archive_sandbox.__get__(m, SandboxManager)
    m.restart_from_archived = SandboxManager.restart_from_archived.__get__(m, SandboxManager)
    m._reconcile_archiving = SandboxManager._reconcile_archiving.__get__(m, SandboxManager)
    return m


class TestArchiveNotConfigured:
    async def test_archive_sandbox_raises_error(self, manager_no_archive):
        with pytest.raises(BadRequestRockError, match="archive not configured"):
            await manager_no_archive.archive_sandbox("sbx-1")

    async def test_restart_from_archived_raises_error(self, manager_no_archive):
        with pytest.raises(BadRequestRockError, match="archive not configured"):
            await manager_no_archive.restart_from_archived("sbx-1")

    async def test_reconcile_archiving_skips(self, manager_no_archive):
        await manager_no_archive._reconcile_archiving()
        manager_no_archive._meta_store.list_by.assert_not_called()


class TestArchiveOperatorNotSupported:
    async def test_archive_sandbox_raises_error(self, manager_no_archive):
        manager_no_archive._dir_storage = AsyncMock()
        manager_no_archive._image_storage = AsyncMock()
        manager_no_archive._operator.supports_archive.return_value = False
        with pytest.raises(BadRequestRockError, match="archive not supported"):
            await manager_no_archive.archive_sandbox("sbx-1")

    async def test_reconcile_archiving_skips(self, manager_no_archive):
        manager_no_archive._operator.supports_archive.return_value = False
        await manager_no_archive._reconcile_archiving()
        manager_no_archive._meta_store.list_by.assert_not_called()
