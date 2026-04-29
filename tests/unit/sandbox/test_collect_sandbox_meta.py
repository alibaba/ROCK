"""RED-GREEN test: _collect_sandbox_meta must read image from meta_store.

Before the fix, BaseManager._collect_sandbox_meta read image from the
in-memory dict ``_sandbox_meta`` which was never populated by ``start_async``,
so image was always "default".  After the fix it reads from meta_store.
"""

from unittest.mock import patch

import pytest

from rock.actions.sandbox.response import State
from rock.admin.core.sandbox_table import SandboxTable
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.utils.providers.redis_provider import RedisProvider

SANDBOX_ID = "sbx-image-001"
EXPECTED_IMAGE = "python:3.11-slim"

SANDBOX_INFO = {
    "sandbox_id": SANDBOX_ID,
    "image": EXPECTED_IMAGE,
    "user_id": "u1",
    "state": State.RUNNING,
    "host_ip": "10.0.0.1",
}


@pytest.fixture
def meta_store(redis_provider: RedisProvider, _memory_sandbox_table: SandboxTable):
    return SandboxMetaStore(redis_provider=redis_provider, sandbox_table=_memory_sandbox_table)


@pytest.fixture
def base_manager(meta_store):
    """Minimal BaseManager with real meta_store, everything else mocked."""
    from rock.sandbox.base_manager import BaseManager

    with patch.object(BaseManager, "__init__", lambda self, *a, **kw: None):
        mgr = BaseManager.__new__(BaseManager)
        mgr._meta_store = meta_store
        mgr._sandbox_meta = {}  # old code needs this; proves empty dict → "default"
        return mgr


class TestCollectSandboxMeta:
    async def test_image_read_from_meta_store(self, base_manager, meta_store):
        """_collect_sandbox_meta should return the actual image stored in meta_store,
        not 'default'.  This test fails on the old code (empty _sandbox_meta dict)."""
        await meta_store.create(SANDBOX_ID, SANDBOX_INFO)

        cnt, meta = await base_manager._collect_sandbox_meta()

        assert cnt == 1
        assert SANDBOX_ID in meta
        assert meta[SANDBOX_ID]["image"] == EXPECTED_IMAGE

    async def test_missing_image_falls_back_to_default(self, base_manager, meta_store):
        """When sandbox_info has no image field, should fall back to 'default'."""
        info_no_image = {
            "sandbox_id": SANDBOX_ID,
            "state": State.RUNNING,
            "host_ip": "10.0.0.1",
        }
        await meta_store.create(SANDBOX_ID, info_no_image)

        cnt, meta = await base_manager._collect_sandbox_meta()

        assert cnt == 1
        assert meta[SANDBOX_ID]["image"] == "default"

    async def test_no_sandboxes_returns_empty(self, base_manager):
        """No alive sandboxes should return zero count and empty dict."""
        cnt, meta = await base_manager._collect_sandbox_meta()

        assert cnt == 0
        assert meta == {}
