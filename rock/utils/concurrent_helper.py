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


class EventLoopLagMonitor:
    """Monitor asyncio event loop lag by measuring scheduling delays.

    Schedules a periodic callback via ``loop.call_later`` and measures the
    gap between the expected fire time and the actual fire time.  A large
    gap means the event loop is saturated (blocked by CPU work or too many
    ready callbacks).

    Usage::

        monitor = EventLoopLagMonitor(interval=1.0, warn_threshold=0.5)
        monitor.start()   # returns the background Task
        # ... application runs ...
        monitor.stop()
    """

    def __init__(self, interval: float = 1.0, warn_threshold: float = 0.5, logger=None):
        self._interval = interval
        self._warn_threshold = warn_threshold
        self._logger = logger
        self._task: asyncio.Task | None = None
        self._max_lag: float = 0.0
        self._lag_count: int = 0
        self._sample_count: int = 0

    async def _monitor_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            expected = self._interval
            t0 = loop.time()
            await asyncio.sleep(self._interval)
            actual = loop.time() - t0
            lag = actual - expected
            self._sample_count += 1

            if lag > self._max_lag:
                self._max_lag = lag

            if lag > self._warn_threshold:
                self._lag_count += 1
                if self._logger:
                    self._logger.warning(
                        f"[event_loop_lag] lag={lag:.3f}s (max={self._max_lag:.3f}s, "
                        f"samples={self._sample_count}, lag_count={self._lag_count})"
                    )

    def start(self) -> asyncio.Task:
        """Start the background monitor task. Returns the asyncio.Task."""
        self._task = asyncio.create_task(self._monitor_loop())
        return self._task

    def stop(self):
        """Cancel the background monitor task."""
        if self._task and not self._task.done():
            self._task.cancel()


class StageTimer:
    """Context manager that logs elapsed time for a named stage."""

    def __init__(self, phase: str, description: str, logger):
        self._phase = phase
        self._description = description
        self._logger = logger
        self._start_time = 0.0

    def __enter__(self):
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.perf_counter() - self._start_time
        self._logger.info(f"[{self._phase}] {self._description} took {duration:.3f} s")
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
