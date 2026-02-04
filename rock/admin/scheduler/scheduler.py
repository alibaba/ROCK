# rock/admin/scheduler/scheduler.py
import asyncio
import multiprocessing as mp
import signal
import time
from datetime import datetime, timedelta
from multiprocessing import Process

import pytz
import ray
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_registry import TaskRegistry
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import SchedulerConfig
from rock.logger import init_logger

logger = init_logger(name="scheduler", file_name=SCHEDULER_LOG_NAME)


class WorkerIPCache:
    """Manages Ray worker IP caching with TTL-based expiration."""

    def __init__(self, ray_address: str, ray_namespace: str, cache_ttl: int = 60):
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self.cache_ttl = cache_ttl
        self._cached_ips: list[str] = []
        self._cache_time: float = 0.0

    def _is_cache_expired(self) -> bool:
        """Check if the cache has expired."""
        return (time.time() - self._cache_time) > self.cache_ttl

    def _fetch_worker_ips_from_ray(self) -> list[str]:
        """Connect to Ray and fetch alive worker IPs."""
        logger.info("Refreshing worker IP cache from Ray cluster")

        should_shutdown = False
        if not ray.is_initialized():
            logger.info(f"Ray start init with address[{self.ray_address}] and namespace[{self.ray_namespace}]")
            ray.init(address=self.ray_address, namespace=self.ray_namespace)
            should_shutdown = True
        else:
            logger.info("Ray has already initialized")

        try:
            nodes = ray.nodes()
            alive_ips = []
            for node in nodes:
                if node.get("Alive", False) and node.get("Resources", {}).get("CPU", 0) > 0:
                    ip = node.get("NodeManagerAddress", "").split(":")[0]
                    if ip:
                        alive_ips.append(ip)
            return alive_ips
        finally:
            if should_shutdown:
                ray.shutdown()
                logger.debug("Ray connection closed after fetching worker IPs")

    def refresh(self) -> list[str]:
        """Force refresh the worker IP cache."""
        try:
            self._cached_ips = self._fetch_worker_ips_from_ray()
            self._cache_time = time.time()
            logger.info(f"Worker cache updated, found {len(self._cached_ips)} workers")
            return self._cached_ips
        except Exception as e:
            logger.error(f"Failed to refresh worker cache: {e}")
            if ray.is_initialized():
                try:
                    ray.shutdown()
                except Exception:
                    pass
            return self._cached_ips

    def get_alive_workers(self, force_refresh: bool = False) -> list[str]:
        """Get alive worker IPs, refreshing cache if needed."""
        try:
            if force_refresh or self._is_cache_expired() or not self._cached_ips:
                return self.refresh()
            return self._cached_ips
        except Exception as e:
            logger.error(f"Failed to get alive workers: {e}")
            return self._cached_ips if self._cached_ips else []


class TaskScheduler:
    """Manages task scheduling using APScheduler."""

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        ray_address: str,
        ray_namespace: str,
    ):
        self.scheduler_config = scheduler_config
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self.local_tz = pytz.timezone(env_vars.ROCK_TIME_ZONE)
        self._scheduler: AsyncIOScheduler | None = None
        self._stop_event: asyncio.Event | None = None
        self._worker_cache: WorkerIPCache | None = None

    def _init_worker_cache(self) -> None:
        """Initialize the worker IP cache."""
        self._worker_cache = WorkerIPCache(
            ray_address=self.ray_address,
            ray_namespace=self.ray_namespace,
            cache_ttl=self.scheduler_config.worker_cache_ttl,
        )

    def _register_tasks(self) -> None:
        """Register all tasks from configuration."""
        from rock.admin.scheduler.task_factory import TaskFactory

        TaskFactory.register_all_tasks(self.scheduler_config)

    async def _run_task(self, task: BaseTask) -> None:
        """Run a single task on alive workers."""
        try:
            worker_ips = self._worker_cache.get_alive_workers()
            if worker_ips:
                logger.info(f"Running task '{task.type}' on {len(worker_ips)} workers")
                await task.run(worker_ips)
            else:
                logger.warning(f"No alive workers found for task '{task.type}'")
        except Exception as e:
            logger.error(f"Task '{task.type}' failed: {e}")

    def _add_jobs(self) -> None:
        """Add all registered tasks as scheduler jobs."""
        for task in TaskRegistry.get_all_tasks().values():
            self._scheduler.add_job(
                self._run_task,
                trigger="interval",
                seconds=task.interval_seconds,
                args=[task],
                id=task.type,
                name=task.type,
                replace_existing=True,
                next_run_time=datetime.now(self.local_tz) + timedelta(seconds=2),
            )
            logger.info(f"Added job '{task.type}' with interval {task.interval_seconds}s")

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum, frame):
            logger.info("Received signal, shutting down scheduler")
            if self._stop_event:
                self._stop_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

    async def run(self) -> None:
        """Run the scheduler until stopped."""
        self._init_worker_cache()
        self._register_tasks()

        self._scheduler = AsyncIOScheduler(timezone=self.local_tz)
        self._add_jobs()

        # Pre-cache worker IPs before starting
        self._worker_cache.refresh()

        self._scheduler.start()
        logger.info("Scheduler started")

        self._stop_event = asyncio.Event()
        self._setup_signal_handlers()

        try:
            await self._stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

class SchedulerProcess:
    """Scheduler process manager - runs APScheduler in a separate process."""

    def __init__(self, scheduler_config: SchedulerConfig, ray_address: str, ray_namespace: str):
        self.scheduler_config = scheduler_config
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self._process: Process | None = None
        self._ctx = mp.get_context("spawn")

    @staticmethod
    def _run_scheduler_in_process(
        scheduler_config: SchedulerConfig,
        ray_address: str,
        ray_namespace: str,
    ) -> None:
        """Entry point for running scheduler in a separate process."""
        try:
            task_scheduler = TaskScheduler(scheduler_config, ray_address, ray_namespace)
            asyncio.run(task_scheduler.run())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler process interrupted")

    def start(self) -> None:
        """Start the scheduler process."""
        if self._process and self._process.is_alive():
            logger.warning("Scheduler process is already running")
            return

        self._process = self._ctx.Process(
            target=self._run_scheduler_in_process,
            args=(self.scheduler_config, self.ray_address, self.ray_namespace),
            daemon=True,
        )
        self._process.start()
        logger.info(f"Scheduler process started with PID: {self._process.pid}")

    def stop(self) -> None:
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
