from __future__ import annotations

import asyncio
import threading
import time
from abc import ABC, abstractmethod

from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.monitor import MetricsMonitor
from rock.admin.scheduler.task_base import TaskRunReport, WorkerRunOutcome
from rock.logger import init_logger

logger = init_logger(__name__)

_FLUSH_TIMEOUT_MILLIS = 10_000


class SchedulerMetricsRecorder(ABC):
    """Metrics interface used by the scheduler execution path."""

    @abstractmethod
    def set_scheduler_up(self, up: bool) -> None:
        pass

    @abstractmethod
    def set_registered_task(self, task_type: str, interval_seconds: int, registered: bool) -> None:
        pass

    @abstractmethod
    def record_worker_cache_refresh(
        self,
        *,
        success: bool,
        cache_ttl: int,
        worker_ips: set[str] | None = None,
    ) -> None:
        pass

    @abstractmethod
    def record_task_report(self, report: TaskRunReport) -> None:
        pass

    @abstractmethod
    async def flush_and_wait(self) -> None:
        pass


class NoopSchedulerMetrics(SchedulerMetricsRecorder):
    """Disabled scheduler metrics adapter with no allocation or flush work."""

    __slots__ = ()

    def set_scheduler_up(self, up: bool) -> None:
        pass

    def set_registered_task(self, task_type: str, interval_seconds: int, registered: bool) -> None:
        pass

    def record_worker_cache_refresh(
        self,
        *,
        success: bool,
        cache_ttl: int,
        worker_ips: set[str] | None = None,
    ) -> None:
        pass

    def record_task_report(self, report: TaskRunReport) -> None:
        pass

    async def flush_and_wait(self) -> None:
        pass


NOOP_SCHEDULER_METRICS: SchedulerMetricsRecorder = NoopSchedulerMetrics()


class SchedulerMetrics(SchedulerMetricsRecorder):
    """Owns scheduler metric names, bounded labels, and coalesced event flushes."""

    def __init__(self, monitor: MetricsMonitor):
        self._monitor = monitor
        self._state_lock = threading.Lock()
        self._scheduler_up = False
        self._worker_cache_initialized = False
        self._alive_worker_ips: set[str] = set()
        self._removed_worker_ips: set[str] = set()
        self._worker_cache_last_success_timestamp: float | None = None
        self._worker_cache_ttl: int | None = None
        self._registered_tasks: dict[str, int] = {}
        self._unregistered_task_tombstones: set[str] = set()
        self._removed_task_interval_tombstones: set[str] = set()
        self._flush_requested = False
        self._flush_task: asyncio.Task | None = None
        self._register_metrics()

    def _register_metrics(self) -> None:
        self._monitor.register_counter(
            MetricsConstants.SCHEDULER_WORKER_CACHE_REFRESH_TOTAL,
            "Number of Ray worker cache refreshes",
        )
        self._monitor.register_counter(
            MetricsConstants.SCHEDULER_WORKER_FAILURES_TOTAL,
            "Number of failed scheduler worker invocations by worker IP",
        )
        self._monitor.register_gauge(
            MetricsConstants.SCHEDULER_WORKER_LAST_FAILURE_TIMESTAMP,
            "Unix timestamp of the last failed scheduler invocation by worker IP",
            "s",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_UP,
            self._observe_scheduler_up,
            "Whether the automatic scheduler is running",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_WORKERS_ALIVE,
            self._observe_workers_alive,
            "Number of cached alive Ray workers",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_WORKER_ALIVE,
            self._observe_worker_alive,
            "Cached alive state for a Ray worker",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_WORKER_CACHE_LAST_SUCCESS_TIMESTAMP,
            self._observe_worker_cache_last_success_timestamp,
            "Unix timestamp of the last successful Ray worker cache refresh",
            "s",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_WORKER_CACHE_TTL,
            self._observe_worker_cache_ttl,
            "Ray worker cache TTL",
            "s",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_TASKS_REGISTERED,
            self._observe_registered_tasks,
            "Whether a scheduler task is registered",
        )
        self._monitor.register_observable_gauge(
            MetricsConstants.SCHEDULER_TASK_INTERVAL,
            self._observe_task_intervals,
            "Configured scheduler task interval",
            "s",
        )

    def _observe_scheduler_up(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            return [(1 if self._scheduler_up else 0, {})]

    def _observe_workers_alive(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            if not self._worker_cache_initialized:
                return []
            return [(len(self._alive_worker_ips), {})]

    def _observe_worker_alive(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            alive_worker_ips = self._alive_worker_ips.copy()
            removed_worker_ips = self._removed_worker_ips.copy()
            self._removed_worker_ips.clear()
        observations = [(1, {"worker_ip": worker_ip}) for worker_ip in alive_worker_ips]
        observations.extend((0, {"worker_ip": worker_ip}) for worker_ip in removed_worker_ips)
        return observations

    def _observe_worker_cache_last_success_timestamp(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            if self._worker_cache_last_success_timestamp is None:
                return []
            return [(self._worker_cache_last_success_timestamp, {})]

    def _observe_worker_cache_ttl(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            if self._worker_cache_ttl is None:
                return []
            return [(self._worker_cache_ttl, {})]

    def _observe_registered_tasks(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            registered_task_types = self._registered_tasks.copy()
            unregistered_task_types = self._unregistered_task_tombstones.copy()
            self._unregistered_task_tombstones.clear()
        observations = [(1, {"task_type": task_type}) for task_type in registered_task_types]
        observations.extend((0, {"task_type": task_type}) for task_type in unregistered_task_types)
        return observations

    def _observe_task_intervals(self) -> list[tuple[float, dict[str, str]]]:
        with self._state_lock:
            registered_tasks = self._registered_tasks.copy()
            removed_task_types = self._removed_task_interval_tombstones.copy()
            self._removed_task_interval_tombstones.clear()
        observations = [
            (interval_seconds, {"task_type": task_type}) for task_type, interval_seconds in registered_tasks.items()
        ]
        observations.extend((0, {"task_type": task_type}) for task_type in removed_task_types)
        return observations

    def set_scheduler_up(self, up: bool) -> None:
        with self._state_lock:
            self._scheduler_up = up

    def set_registered_task(self, task_type: str, interval_seconds: int, registered: bool) -> None:
        with self._state_lock:
            if registered:
                self._registered_tasks[task_type] = interval_seconds
                self._unregistered_task_tombstones.discard(task_type)
                self._removed_task_interval_tombstones.discard(task_type)
            else:
                self._registered_tasks.pop(task_type, None)
                self._unregistered_task_tombstones.add(task_type)
                self._removed_task_interval_tombstones.add(task_type)

    def set_alive_workers(self, worker_ips: set[str]) -> None:
        current_ips = worker_ips.copy()
        with self._state_lock:
            self._removed_worker_ips.difference_update(current_ips)
            self._removed_worker_ips.update(self._alive_worker_ips - current_ips)
            self._alive_worker_ips = current_ips
            self._worker_cache_initialized = True

    def record_worker_cache_refresh(
        self,
        *,
        success: bool,
        cache_ttl: int,
        worker_ips: set[str] | None = None,
    ) -> None:
        outcome = "success" if success else "failure"
        self._monitor.record_counter_by_name(
            MetricsConstants.SCHEDULER_WORKER_CACHE_REFRESH_TOTAL,
            1,
            {"outcome": outcome},
        )
        with self._state_lock:
            self._worker_cache_ttl = cache_ttl
            if success:
                self._worker_cache_last_success_timestamp = time.time()
        if success:
            if worker_ips is not None:
                self.set_alive_workers(worker_ips)
        self.request_flush()

    def record_task_report(self, report: TaskRunReport) -> None:
        has_failures = False
        failure_timestamp: float | None = None
        for worker_result in report.worker_results:
            if worker_result.outcome not in {WorkerRunOutcome.FAILED, WorkerRunOutcome.TIMEOUT}:
                continue
            has_failures = True
            if failure_timestamp is None:
                failure_timestamp = time.time()
            attributes = {
                "task_type": report.task_type,
                "worker_ip": worker_result.worker_ip,
            }
            self._monitor.record_counter_by_name(
                MetricsConstants.SCHEDULER_WORKER_FAILURES_TOTAL,
                1,
                attributes,
            )
            self._monitor.record_gauge_by_name(
                MetricsConstants.SCHEDULER_WORKER_LAST_FAILURE_TIMESTAMP,
                failure_timestamp,
                attributes,
            )
        if has_failures:
            self.request_flush()

    def request_flush(self) -> None:
        self._flush_requested = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Cannot request scheduler metric flush without a running event loop")
            return
        self._flush_task = loop.create_task(self._flush_loop())

    async def flush_and_wait(self) -> None:
        self.request_flush()
        task = self._flush_task
        if task is not None:
            await asyncio.shield(task)

    async def _flush_loop(self) -> None:
        try:
            while self._flush_requested:
                self._flush_requested = False
                try:
                    success = await asyncio.to_thread(
                        self._monitor.force_flush,
                        timeout_millis=_FLUSH_TIMEOUT_MILLIS,
                    )
                except Exception:
                    logger.exception("Scheduler metric force flush failed")
                else:
                    if not success:
                        logger.warning("Scheduler metric force flush returned false")
        finally:
            self._flush_task = None
