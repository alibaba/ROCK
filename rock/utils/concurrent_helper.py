import asyncio
import atexit
import concurrent.futures
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

_thread_pool = None
_thread_pool_lock = threading.Lock()
_global_executor: ThreadPoolExecutor | None = None
MAX_WORKERS = 300


def _get_thread_pool():
    """Get global thread pool"""
    global _thread_pool
    if _thread_pool is None:
        with _thread_pool_lock:
            if _thread_pool is None:
                _thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="AsyncRunner")
                # Register cleanup function
                atexit.register(_cleanup_thread_pool)
    return _thread_pool


def _cleanup_thread_pool():
    """Clean up thread pool"""
    global _thread_pool
    if _thread_pool is not None:
        _thread_pool.shutdown(wait=True)
        _thread_pool = None


def _run_in_new_loop(coro):
    """Run coroutine in new thread"""
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def run_until_complete(coro):
    """Run coroutine until completion, handling nested event loop situations"""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            # Use reused thread pool
            executor = _get_thread_pool()
            future = executor.submit(_run_in_new_loop, coro)
            return future.result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError as e:
        if "no running event loop" in str(e):
            return asyncio.run(coro)
        else:
            # Use reused thread pool
            executor = _get_thread_pool()
            future = executor.submit(_run_in_new_loop, coro)
            return future.result()


def get_executor() -> ThreadPoolExecutor:
    """Get global thread pool executor"""
    global _global_executor
    if _global_executor is None:
        _global_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    return _global_executor


class Timer:
    """Simple execution timer context manager"""

    def __init__(self, description="Execution"):
        self.description = description
        self.start_time = 0.0

    def __enter__(self):
        # Record start time when entering with block
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Calculate and print time when exiting with block
        end_time = time.perf_counter()
        duration_ms = end_time - self.start_time
        print(f"{self.description} took {duration_ms:.3f} s")
        # Return False means if there's an exception in the with block, throw it normally
        return False


class AsyncSafeDict(Generic[K, V]):
    """Thread-safe async dictionary"""

    def __init__(self):
        self._dict = {}
        self._lock = asyncio.Lock()

    async def get(self, key: K, default: V | None = None):
        async with self._lock:
            return self._dict.get(key, default)

    async def set(self, key: K, value: V):
        async with self._lock:
            self._dict[key] = value

    async def pop(self, key: K, default: V | None = None):
        async with self._lock:
            return self._dict.pop(key, default)

    async def keys(self):
        async with self._lock:
            return self._dict.keys()

    def __len__(self):
        return len(self._dict)


class AsyncAtomicInt:
    """Thread-safe async atomic integer"""

    def __init__(self, value: int = 0):
        self._value: int = value
        self._lock = asyncio.Lock()

    async def inc(self) -> int:
        async with self._lock:
            self._value += 1
            return self._value

    async def get(self) -> int:
        async with self._lock:
            return self._value


@contextmanager
def timeout(seconds):
    """Context manager for setting execution timeout"""

    def timeout_handler(signum, frame):
        raise TimeoutError(f"timeout({seconds} seconds)")

    # set a signal
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)  # cancel the alarm
        signal.signal(signal.SIGALRM, old_handler)


class RayUtil:
    """Ray distributed computing utilities"""

    @staticmethod
    async def get_alive_worker_nodes():
        """Get alive worker nodes in Ray cluster"""
        import ray

        nodes = ray.nodes()
        alive_nodes = [node for node in nodes if node["Alive"]]
        alive_worker_nodes = [node for node in alive_nodes if node["Resources"].get("node:__internal_head__", 0) != 1.0]
        return alive_worker_nodes

    @staticmethod
    async def async_ray_get(ray_future):
        """Async wrapper for ray.get"""
        import ray

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(get_executor(), ray.get, ray_future)
