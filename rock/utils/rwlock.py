import asyncio
from contextlib import asynccontextmanager


class AsyncRWLock:
    """
    An asynchronous reader-writer lock implementation that allows multiple concurrent readers
    or exclusive writers. This lock ensures that:
    
    - Multiple readers can acquire the lock simultaneously when no writer is active
    - Writers have exclusive access when acquiring the lock (no readers or other writers)
    - Writers are prioritized over new readers when a writer is waiting, preventing reader starvation
    - The lock is awaitable and integrates with Python's asyncio framework
    
    The implementation uses asyncio.Condition to manage the waiting queue and coordinate
    between readers and writers safely in an asynchronous context.
    
    Usage:
        lock = AsyncRWLock()
        
        # For read operations (shared access)
        async with lock.read_lock():
            # Multiple coroutines can enter this block simultaneously
            data = await read_data()
            
        # For write operations (exclusive access)
        async with lock.write_lock():
            # Only one coroutine can enter this block at a time
            await write_data(new_data)
    """
    def __init__(self):
        self._readers = 0
        self._writer = False
        self._writer_waiting = 0
        self._cond = asyncio.Condition()

    async def acquire_read(self):
        async with self._cond:
            while self._writer or self._writer_waiting > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self):
        async with self._cond:
            self._readers -= 1
            if self._readers < 0:
                raise RuntimeError("release_read called more times than acquire_read")
            if self._readers == 0:
                self._cond.notify_all()

    @asynccontextmanager
    async def read_lock(self):
        await self.acquire_read()
        try:
            yield
        finally:
            await self.release_read()

    async def acquire_write(self):
        async with self._cond:
            self._writer_waiting += 1
            try:
                while self._writer or self._readers > 0:
                    await self._cond.wait()
                self._writer = True
            finally:
                self._writer_waiting -= 1

    async def release_write(self):
        async with self._cond:
            if not self._writer:
                raise RuntimeError("release_write called without a writer")
            self._writer = False
            self._cond.notify_all()

    @asynccontextmanager
    async def write_lock(self):
        await self.acquire_write()
        try:
            yield
        finally:
            await self.release_write()
