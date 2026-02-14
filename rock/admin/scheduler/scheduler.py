# rock/admin/scheduler/scheduler.py
import asyncio
import hashlib
import json
import logging
import multiprocessing as mp
import signal
import time
from datetime import datetime, timedelta
from multiprocessing import Process

import pytz
import ray
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_registry import TaskRegistry
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import NacosConfig, SchedulerConfig, TaskConfig
from rock.logger import init_logger
from rock.utils.providers import NacosConfigProvider

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
    """Manages task scheduling using APScheduler with optional Nacos dynamic config."""

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        ray_address: str,
        ray_namespace: str,
        nacos_config: NacosConfig | None = None,
    ):
        self.scheduler_config = scheduler_config
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self.nacos_config = nacos_config
        self.local_tz = pytz.timezone(env_vars.ROCK_TIME_ZONE)
        self._scheduler: AsyncIOScheduler | None = None
        self._stop_event: asyncio.Event | None = None
        self._worker_cache: WorkerIPCache | None = None
        self._nacos_provider: NacosConfigProvider | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._last_scheduler_config_hash: str | None = None

    def _init_worker_cache(self) -> None:
        """Initialize the worker IP cache."""
        self._worker_cache = WorkerIPCache(
            ray_address=self.ray_address,
            ray_namespace=self.ray_namespace,
            cache_ttl=self.scheduler_config.worker_cache_ttl,
        )

    def _init_nacos_provider(self) -> None:
        """Initialize Nacos config provider and register listener for scheduler config changes."""
        if not self.nacos_config or not self.nacos_config.endpoint:
            logger.info("Nacos config not provided, skipping Nacos dynamic config")
            return

        self._nacos_provider = NacosConfigProvider(
            endpoint=self.nacos_config.endpoint,
            namespace="",
            data_id=self.nacos_config.data_id,
            group=self.nacos_config.group,
        )

        # Override the default update callback with our scheduler-aware callback
        self._nacos_provider._update_callback = self._on_nacos_config_changed
        self._nacos_provider.add_listener()

        # Suppress noisy nacos logs
        logging.getLogger("nacos.client").setLevel(logging.WARNING)
        logging.getLogger("do-pulling").setLevel(logging.WARNING)
        logging.getLogger("process-polling-result").setLevel(logging.WARNING)

        logger.info("Nacos dynamic config listener registered for scheduler")

    def _load_scheduler_config_from_nacos(self) -> SchedulerConfig | None:
        """Synchronously load scheduler config from Nacos."""
        if not self._nacos_provider:
            return None

        try:
            config_str = self._nacos_provider.client.get_config(
                self._nacos_provider.data_id, self._nacos_provider.group
            )
            if not config_str:
                return None

            config_dict = yaml.safe_load(config_str)
            if not config_dict or "scheduler" not in config_dict:
                logger.warning("No 'scheduler' section found in Nacos config")
                return None

            # Initialize the config hash so subsequent change callbacks can skip if unchanged
            scheduler_raw = json.dumps(config_dict["scheduler"], sort_keys=True)
            self._last_scheduler_config_hash = hashlib.md5(scheduler_raw.encode()).hexdigest()

            nacos_scheduler_config = SchedulerConfig(**config_dict["scheduler"])
            logger.info(f"Loaded scheduler config from Nacos with {len(nacos_scheduler_config.tasks)} tasks")
            return nacos_scheduler_config
        except Exception as e:
            logger.error(f"Failed to load scheduler config from Nacos: {e}")
            return None

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
        """Reload scheduler config: handle enabled, worker_cache_ttl, and task changes."""
        old_scheduler_config = self.scheduler_config
        self.scheduler_config = new_scheduler_config

        # Handle enabled change: disabled
        if old_scheduler_config.enabled and not new_scheduler_config.enabled:
            self._pause_all_tasks()
            logger.info("Scheduler disabled via Nacos config, all tasks paused")
            return

        # Handle worker_cache_ttl change
        if old_scheduler_config.worker_cache_ttl != new_scheduler_config.worker_cache_ttl:
            self._worker_cache.cache_ttl = new_scheduler_config.worker_cache_ttl
            logger.info(
                f"Updated worker_cache_ttl: "
                f"{old_scheduler_config.worker_cache_ttl}s -> {new_scheduler_config.worker_cache_ttl}s"
            )

        # Handle task changes (only if scheduler is enabled)
        newly_added_tasks: set[str] = set()
        if new_scheduler_config.enabled:
            newly_added_tasks = await self._reload_tasks(new_scheduler_config)

        # Handle enabled change: re-enabled
        # Resume AFTER task reload, excluding newly added tasks (already scheduled by _reload_tasks)
        if not old_scheduler_config.enabled and new_scheduler_config.enabled:
            self._resume_all_tasks(exclude_task_types=newly_added_tasks)
            logger.info("Scheduler re-enabled via Nacos config, all tasks resumed")

    def _pause_all_tasks(self) -> None:
        """Pause all scheduled jobs by removing them from the scheduler."""
        for task_type in list(TaskRegistry.get_all_tasks().keys()):
            try:
                self._scheduler.pause_job(task_type)
                logger.info(f"Paused task '{task_type}'")
            except Exception as e:
                logger.warning(f"Failed to pause job '{task_type}': {e}")

    def _resume_all_tasks(self, exclude_task_types: set[str] | None = None) -> None:
        """Resume paused jobs and trigger immediate execution, excluding specified tasks."""
        excluded = exclude_task_types or set()
        for index, task_type in enumerate(TaskRegistry.get_all_tasks().keys()):
            if task_type in excluded:
                continue
            try:
                self._scheduler.resume_job(task_type)
                job = self._scheduler.get_job(task_type)
                if job:
                    job.modify(next_run_time=datetime.now(self.local_tz) + timedelta(seconds=index * 60 + 2))
                logger.info(f"Resumed task '{task_type}' with next run in {index * 60 + 2}s")
            except Exception as e:
                logger.warning(f"Failed to resume job '{task_type}': {e}")

    @staticmethod
    def _task_params_changed(existing_task: BaseTask, new_task: BaseTask) -> bool:
        """Compare subclass-specific attributes to detect parameter changes."""
        base_attrs = {"type", "interval_seconds", "idempotency", "status_file_path", "_executor"}
        existing_params = {k: v for k, v in existing_task.__dict__.items() if k not in base_attrs}
        new_params = {k: v for k, v in new_task.__dict__.items() if k not in base_attrs}
        return existing_params != new_params

    async def _reload_tasks(self, new_scheduler_config: SchedulerConfig) -> set[str]:
        """Reload scheduler tasks based on new config. Returns the set of newly added task types."""
        from rock.admin.scheduler.task_factory import TaskFactory

        # Build a map of new task configs keyed by task_class
        new_task_configs: dict[str, TaskConfig] = {}
        for task_config in new_scheduler_config.tasks:
            if task_config.enabled and task_config.task_class:
                new_task_configs[task_config.task_class] = task_config

        current_tasks = TaskRegistry.get_all_tasks()
        current_task_types = set(current_tasks.keys())

        # Build new tasks to determine which types will exist
        new_tasks_by_type: dict[str, tuple[BaseTask, TaskConfig]] = {}
        for task_class_path, task_config in new_task_configs.items():
            try:
                task = TaskFactory.create_task(task_config)
                new_tasks_by_type[task.type] = (task, task_config)
            except Exception as e:
                logger.error(f"Failed to create task from config '{task_class_path}': {e}")

        new_task_types = set(new_tasks_by_type.keys())

        # Remove tasks that are no longer in config
        tasks_to_remove = current_task_types - new_task_types
        for task_type in tasks_to_remove:
            TaskRegistry.unregister(task_type)
            try:
                self._scheduler.remove_job(task_type)
                logger.info(f"Removed task '{task_type}'")
            except Exception as e:
                logger.warning(f"Failed to remove job '{task_type}': {e}")

        # Add or update tasks
        tasks_to_add = new_task_types - current_task_types
        tasks_to_update = new_task_types & current_task_types

        for index, task_type in enumerate(tasks_to_add):
            task, task_config = new_tasks_by_type[task_type]
            TaskRegistry.register(task)
            start_delay = index * 60 + 2
            self._scheduler.add_job(
                self._run_task,
                trigger="interval",
                seconds=task.interval_seconds,
                args=[task],
                id=task.type,
                name=task.type,
                replace_existing=True,
                next_run_time=datetime.now(self.local_tz) + timedelta(seconds=start_delay),
            )
            logger.info(
                f"Added new task '{task_type}' with interval {task.interval_seconds}s, first run in {start_delay}s"
            )

        actually_updated = 0
        immediately_scheduled_tasks: set[str] = set()
        # Start delay offset continues from where tasks_to_add left off
        rerun_delay_index = len(tasks_to_add)

        for task_type in tasks_to_update:
            existing_task = current_tasks[task_type]
            new_task, task_config = new_tasks_by_type[task_type]

            # Check if task config actually changed
            interval_changed = existing_task.interval_seconds != new_task.interval_seconds
            params_changed = self._task_params_changed(existing_task, new_task)

            if not interval_changed and not params_changed:
                continue

            actually_updated += 1
            TaskRegistry.unregister(task_type)
            TaskRegistry.register(new_task)
            job = self._scheduler.get_job(task_type)
            if job:
                job.modify(args=[new_task])

            if interval_changed:
                self._scheduler.reschedule_job(
                    task_type,
                    trigger="interval",
                    seconds=new_task.interval_seconds,
                )
                logger.info(
                    f"Updated task '{task_type}' interval: "
                    f"{existing_task.interval_seconds}s -> {new_task.interval_seconds}s"
                )

            if params_changed and job:
                start_delay = rerun_delay_index * 60 + 2
                rerun_delay_index += 1
                job.modify(next_run_time=datetime.now(self.local_tz) + timedelta(seconds=start_delay))
                immediately_scheduled_tasks.add(task_type)
                logger.info(f"Updated task '{task_type}' params, scheduled re-run in {start_delay}s")

        logger.info(
            f"Task reload complete: added={len(tasks_to_add)}, "
            f"removed={len(tasks_to_remove)}, updated={actually_updated}, "
            f"unchanged={len(tasks_to_update) - actually_updated}"
        )
        return tasks_to_add | immediately_scheduled_tasks

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
        self._event_loop = asyncio.get_running_loop()

        self._init_worker_cache()

        # Try loading scheduler config from Nacos first, fall back to local config
        self._init_nacos_provider()
        nacos_scheduler_config = self._load_scheduler_config_from_nacos()
        if nacos_scheduler_config:
            self.scheduler_config = nacos_scheduler_config
            logger.info("Using scheduler config from Nacos")
        else:
            logger.info("Using local scheduler config (Nacos not available or no scheduler section)")

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

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        ray_address: str,
        ray_namespace: str,
        nacos_config: "NacosConfig | None" = None,
    ):
        self.scheduler_config = scheduler_config
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self.nacos_config = nacos_config
        self._process: Process | None = None
        self._ctx = mp.get_context("spawn")

    @staticmethod
    def _run_scheduler_in_process(
        scheduler_config: SchedulerConfig,
        ray_address: str,
        ray_namespace: str,
        nacos_config: "NacosConfig | None" = None,
    ) -> None:
        """Entry point for running scheduler in a separate process."""
        try:
            task_scheduler = TaskScheduler(scheduler_config, ray_address, ray_namespace, nacos_config)
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
            args=(self.scheduler_config, self.ray_address, self.ray_namespace, self.nacos_config),
            daemon=False,
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
