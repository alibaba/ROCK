# rock/admin/scheduler/scheduler.py
import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta

import pytz
import ray
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_registry import TaskRegistry
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import SchedulerConfig
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
        self._rebuild_tasks()

    def _rebuild_tasks(self) -> None:
        """Clear all jobs and re-register from current config."""
        self._scheduler.remove_all_jobs()
        TaskRegistry.clear()

        if not self.scheduler_config.enabled:
            logger.info("Scheduler disabled, all tasks removed")
            return

        self._register_tasks()
        self._add_jobs()
        logger.info("Scheduler tasks rebuilt")

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
        for index, task in enumerate(TaskRegistry.get_all_tasks().values()):
            self._scheduler.add_job(
                self._run_task,
                trigger="interval",
                seconds=task.interval_seconds,
                args=[task],
                id=task.type,
                name=task.type,
                replace_existing=True,
                next_run_time=datetime.now(self.local_tz) + timedelta(seconds=index * 60 + 2),
            )
            logger.info(f"Added job '{task.type}' with interval {task.interval_seconds}s")

    async def run(self) -> None:
        """Run the scheduler until stopped."""
        self._event_loop = asyncio.get_running_loop()

        self._init_worker_cache()

        if self._nacos_provider:
            self._nacos_provider.add_listener(self._on_nacos_config_changed)
            logger.info("Nacos dynamic config listener registered for scheduler")

        self._register_tasks()

        self._scheduler = AsyncIOScheduler(timezone=self.local_tz)
        self._add_jobs()

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
