# rock/admin/scheduler/scheduler.py
import asyncio
import hashlib
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta

import pytz
import ray
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import SchedulerConfig, TaskConfig
from rock.logger import init_logger
from rock.utils.providers import NacosConfigProvider

logger = init_logger(name="scheduler", file_name=SCHEDULER_LOG_NAME)


class WorkerIPCache:
    """Manages Ray worker IP caching with TTL-based expiration."""

    def __init__(self, cache_ttl: int = 60):
        self.cache_ttl = cache_ttl
        self._cached_ips: list[str] = []
        self._cache_time: float = 0.0

    def _is_cache_expired(self) -> bool:
        """Check if the cache has expired."""
        return (time.time() - self._cache_time) > self.cache_ttl

    def _fetch_worker_ips_from_ray(self) -> list[str]:
        """Fetch alive worker IPs from the already-initialized Ray cluster."""
        logger.info("Refreshing worker IP cache from Ray cluster")
        nodes = ray.nodes()
        alive_ips = []
        for node in nodes:
            if node.get("Alive", False) and node.get("Resources", {}).get("CPU", 0) > 0:
                ip = node.get("NodeManagerAddress", "").split(":")[0]
                if ip:
                    alive_ips.append(ip)
        return alive_ips

    def refresh(self) -> list[str]:
        """Force refresh the worker IP cache."""
        try:
            self._cached_ips = self._fetch_worker_ips_from_ray()
            self._cache_time = time.time()
            logger.info(f"Worker cache updated, found {len(self._cached_ips)} workers")
            return self._cached_ips
        except Exception as e:
            logger.error(f"Failed to refresh worker cache: {e}")
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
    """Manages task scheduling using APScheduler with optional Nacos dynamic config."""

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        nacos_provider: NacosConfigProvider | None = None,
    ):
        self.scheduler_config = scheduler_config
        self.local_tz = pytz.timezone(env_vars.ROCK_TIME_ZONE)
        self._scheduler: AsyncIOScheduler | None = None
        self._stop_event: asyncio.Event | None = None
        self._worker_cache: WorkerIPCache | None = None
        self._nacos_provider = nacos_provider
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._last_scheduler_config_hash: str | None = None
        self._task_hashes: dict[str, str] = {}
        self._tasks_by_class: dict[str, BaseTask] = {}

    def _init_worker_cache(self) -> None:
        """Initialize the worker IP cache."""
        self._worker_cache = WorkerIPCache(
            cache_ttl=self.scheduler_config.worker_cache_ttl,
        )

    def _on_nacos_config_changed(self, new_config: dict) -> None:
        """Callback invoked by Nacos watcher when config changes (runs in Nacos polling thread)."""
        logger.info("Nacos config changed, checking scheduler section...")
        try:
            config_dict = yaml.safe_load(new_config["content"])
            if not config_dict or "scheduler" not in config_dict:
                logger.warning("No 'scheduler' section in updated Nacos config, skipping")
                return

            # Compare scheduler section hash to avoid unnecessary reloads
            scheduler_raw = json.dumps(config_dict["scheduler"], sort_keys=True)
            config_hash = hashlib.md5(scheduler_raw.encode()).hexdigest()
            if config_hash == self._last_scheduler_config_hash:
                logger.info("Scheduler config unchanged, skipping reload")
                return
            self._last_scheduler_config_hash = config_hash

            new_scheduler_config = SchedulerConfig(**config_dict["scheduler"])
            # Schedule the async reload on the event loop (thread-safe)
            if self._event_loop and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(self._reload_scheduler_config(new_scheduler_config), self._event_loop)
            else:
                logger.warning("Event loop not available, cannot reload tasks dynamically")
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse updated Nacos YAML config: {e}")
        except Exception as e:
            logger.error(f"Failed to process Nacos config change: {e}")

    async def _reload_scheduler_config(self, new_scheduler_config: SchedulerConfig) -> None:
        """Reload scheduler config by clearing and rebuilding all tasks."""
        self.scheduler_config = new_scheduler_config
        self._worker_cache.cache_ttl = new_scheduler_config.worker_cache_ttl
        await self._rebuild_tasks()

    @staticmethod
    def _compute_task_hash(task_config: TaskConfig) -> str:
        raw = json.dumps(asdict(task_config), sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def _install_task(self, task_config: TaskConfig) -> None:
        """Create, register, and schedule a single task."""
        from rock.admin.scheduler.task_factory import TaskFactory

        try:
            task = TaskFactory.create_task(task_config)
        except Exception as e:
            logger.error(f"Failed to create task '{task_config.task_class}': {e}")
            return

        self._tasks_by_class[task_config.task_class] = task
        self._task_hashes[task_config.task_class] = self._compute_task_hash(task_config)
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
        logger.info(f"Installed task '{task.type}' with interval {task.interval_seconds}s")

    async def _uninstall_task(self, task_class: str) -> None:
        """Remove a single task from the scheduler and clean up its worker-side processes."""
        task = self._tasks_by_class.pop(task_class, None)
        self._task_hashes.pop(task_class, None)
        if task is None:
            return
        try:
            self._scheduler.remove_job(task.type)
        except Exception as e:
            logger.warning(f"Failed to remove scheduler job '{task.type}': {e}")
        if self._worker_cache is not None:
            worker_ips = self._worker_cache.get_alive_workers()
            if worker_ips:
                await task.cleanup(worker_ips)
        logger.info(f"Uninstalled task '{task.type}'")

    async def _rebuild_tasks(self) -> None:
        """Apply config changes by diffing old vs new tasks; only touch the ones that changed."""
        if not self.scheduler_config.enabled:
            for task_class in list(self._tasks_by_class):
                await self._uninstall_task(task_class)
            logger.info("Scheduler disabled, all tasks removed")
            return

        new_by_class: dict[str, TaskConfig] = {}
        new_hashes: dict[str, str] = {}
        for task_config in self.scheduler_config.tasks:
            if not task_config.enabled or not task_config.task_class:
                continue
            new_by_class[task_config.task_class] = task_config
            new_hashes[task_config.task_class] = self._compute_task_hash(task_config)

        old_keys = set(self._task_hashes)
        new_keys = set(new_hashes)
        removed = old_keys - new_keys
        added = new_keys - old_keys
        changed = {k for k in (old_keys & new_keys) if self._task_hashes[k] != new_hashes[k]}

        for task_class in removed | changed:
            await self._uninstall_task(task_class)
        for task_class in added | changed:
            self._install_task(new_by_class[task_class])

        if removed or added or changed:
            logger.info(f"Scheduler tasks updated: removed={len(removed)}, added={len(added)}, changed={len(changed)}")
        else:
            logger.info("No task changes detected")

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

    async def run(self) -> None:
        """Run the scheduler until stopped."""
        self._event_loop = asyncio.get_running_loop()

        self._init_worker_cache()

        if self._nacos_provider:
            self._nacos_provider.add_listener(self._on_nacos_config_changed)
            logger.info("Nacos dynamic config listener registered for scheduler")

        self._scheduler = AsyncIOScheduler(timezone=self.local_tz)
        await self._rebuild_tasks()

        # Pre-cache worker IPs before starting
        self._worker_cache.refresh()

        self._scheduler.start()
        logger.info("Scheduler started")

        self._stop_event = asyncio.Event()

        try:
            await self._stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def stop(self) -> None:
        """Thread-safe stop: signal the scheduler to shut down."""
        if self._stop_event and self._event_loop:
            self._event_loop.call_soon_threadsafe(self._stop_event.set)


class SchedulerThread:
    """Scheduler thread manager - runs APScheduler in a daemon thread with its own event loop."""

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        nacos_provider: NacosConfigProvider | None = None,
    ):
        self.scheduler_config = scheduler_config
        self.nacos_provider = nacos_provider
        self._thread: threading.Thread | None = None
        self._task_scheduler: TaskScheduler | None = None

    def _run_scheduler_in_thread(self) -> None:
        """Entry point for running scheduler in a thread with a dedicated event loop."""
        try:
            self._task_scheduler = TaskScheduler(self.scheduler_config, self.nacos_provider)
            asyncio.run(self._task_scheduler.run())
        except Exception:
            logger.exception("Scheduler thread encountered an error")

    def start(self) -> None:
        """Start the scheduler thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduler thread is already running")
            return

        self._thread = threading.Thread(
            target=self._run_scheduler_in_thread,
            name="scheduler-thread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Scheduler thread started")

    def stop(self) -> None:
        """Stop the scheduler thread gracefully."""
        if self._task_scheduler:
            self._task_scheduler.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
            logger.info("Scheduler thread stopped")

    def is_alive(self) -> bool:
        """Check if the scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()
