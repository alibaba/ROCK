# rock/admin/scheduler/scheduler.py
import asyncio
import multiprocessing as mp
import signal
import time
from multiprocessing import Process

import ray
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_registry import TaskRegistry
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import SchedulerConfig
from rock.logger import init_logger

logger = init_logger(name="scheduler", file_name=SCHEDULER_LOG_NAME)


# Worker IP cache (module-level, used in subprocess)
_worker_cache_ips: list[str] = []
_worker_cache_time: float = 0.0
_worker_cache_ttl: int = 60


def _get_alive_workers(ray_address: str, ray_namespace: str, force_refresh: bool = False) -> list[str]:
    """Get alive worker IPs from Ray cluster with caching."""
    global _worker_cache_ips, _worker_cache_time

    try:
        current_time = time.time()
        cache_expired = (current_time - _worker_cache_time) > _worker_cache_ttl

        if force_refresh or cache_expired or not _worker_cache_ips:
            logger.info("Refreshing worker IP cache from Ray cluster")

            # Track if connection was initialized by this call
            should_shutdown = False
            if not ray.is_initialized():
                logger.info(f"Ray start init with address[{ray_address}] and namespace[{ray_namespace}]")
                ray.init(address=ray_address, namespace=ray_namespace)
                should_shutdown = True
            else:
                logger.info("Ray has already inintialized")

            try:
                nodes = ray.nodes()
                alive_ips = []
                for node in nodes:
                    if node.get("Alive", False) and node.get("Resources", {}).get("CPU", 0) > 0:
                        ip = node.get("NodeManagerAddress", "").split(":")[0]
                        if ip:
                            alive_ips.append(ip)

                _worker_cache_ips = alive_ips
                _worker_cache_time = current_time
                logger.info(f"Worker cache updated, found {len(_worker_cache_ips)} workers")
            finally:
                # Disconnect if initialized by this call to reduce Ray head load
                if should_shutdown:
                    ray.shutdown()
                    logger.debug("Ray connection closed after fetching worker IPs")

        return _worker_cache_ips
    except Exception as e:
        logger.error(f"Failed to get alive workers: {e}")
        # Ensure connection cleanup on exception
        if ray.is_initialized():
            try:
                ray.shutdown()
            except Exception:
                pass
        return _worker_cache_ips if _worker_cache_ips else []


async def _execute_task(task: BaseTask, ray_address: str, ray_namespace: str):
    """Execute a single task."""
    try:
        worker_ips = _get_alive_workers(ray_address, ray_namespace)
        if worker_ips:
            logger.info(f"Running task '{task.name}' on {len(worker_ips)} workers")
            await task.run(worker_ips)
        else:
            logger.warning(f"No alive workers found for task '{task.name}'")
    except Exception as e:
        logger.error(f"Task '{task.name}' failed: {e}")


async def _run_scheduler_async(
    scheduler_config: SchedulerConfig,
    ray_address: str,
    ray_namespace: str,
):
    """Core async logic for running the scheduler."""
    global _worker_cache_ttl
    _worker_cache_ttl = scheduler_config.worker_cache_ttl

    scheduler = AsyncIOScheduler()

    # Register tasks in subprocess
    from rock.admin.scheduler.task_factory import TaskFactory

    TaskFactory.register_all_tasks(scheduler_config)

    # Add all tasks to scheduler
    for task in TaskRegistry.get_all_tasks().values():
        scheduler.add_job(
            _execute_task,
            trigger="interval",
            seconds=task.interval_seconds,
            args=[task, ray_address, ray_namespace],
            id=task.name,
            name=task.name,
            replace_existing=True,
        )
        logger.info(f"Added job '{task.name}' with interval {task.interval_seconds}s")

    # Start scheduler in event loop
    scheduler.start()
    logger.info("Scheduler started")

    # Create event for graceful shutdown
    stop_event = asyncio.Event()

    def signal_handler(signum, frame):
        logger.info("Received signal, shutting down scheduler")
        stop_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Wait for stop signal
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def _run_scheduler_in_process(
    scheduler_config: SchedulerConfig,
    ray_address: str,
    ray_namespace: str,
):
    """Run scheduler in a separate process."""
    try:
        asyncio.run(_run_scheduler_async(scheduler_config, ray_address, ray_namespace))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler process interrupted")


class SchedulerProcess:
    """Scheduler process manager - runs APScheduler in a separate process."""

    def __init__(self, scheduler_config: SchedulerConfig, ray_address: str, ray_namespace: str):
        self.scheduler_config = scheduler_config
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self._process: Process | None = None
        # Use spawn to avoid inheriting Ray connection state from parent
        self._ctx = mp.get_context("spawn")

    def start(self):
        """Start the scheduler process."""
        if self._process and self._process.is_alive():
            logger.warning("Scheduler process is already running")
            return

        # Use spawn context so subprocess won't inherit parent's Ray state
        self._process = self._ctx.Process(
            target=_run_scheduler_in_process,
            args=(self.scheduler_config, self.ray_address, self.ray_namespace),
            daemon=True,
        )
        self._process.start()
        logger.info(f"Scheduler process started with PID: {self._process.pid}")

    def stop(self):
        """Stop the scheduler process."""
        if self._process and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2)
            logger.info("Scheduler process stopped")

    def is_alive(self) -> bool:
        """Check if the scheduler process is alive."""
        return self._process is not None and self._process.is_alive()
