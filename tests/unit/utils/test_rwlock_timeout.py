import asyncio

import pytest

from rock import InternalServerRockError
from rock.utils.rwlock import AsyncRWLock


@pytest.mark.asyncio
async def test_rwlock_write_timeout_then_read_lock_ok():
    lock = AsyncRWLock()

    lock._readers = 1

    async def writer():
        with pytest.raises(InternalServerRockError):
            async with lock.write_lock(timeout=5):
                pytest.fail("write_lock should have timed out and not enter this block")

    await writer()

    assert lock._writer is False
    assert lock._writer_waiting == 0

    lock._readers = 0

    acquired = False

    async def reader():
        nonlocal acquired
        async with lock.read_lock():
            acquired = True
            await asyncio.sleep(1)

    await reader()

    assert acquired is True, "Read lock should be obtainable after write timeout"
