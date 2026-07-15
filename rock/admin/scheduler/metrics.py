from __future__ import annotations

import asyncio
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
        self._alive_worker_ips: set[str] = set()
        self._flush_requested = False
        self._flush_task: asyncio.Task | None = None

    def set_scheduler_up(self, up: bool) -> None:
        self._monitor.record_gauge_by_name(MetricsConstants.SCHEDULER_UP, 1 if up else 0)

    def set_registered_task(self, task_type: str, interval_seconds: int, registered: bool) -> None:
        attributes = {"task_type": task_type}
        self._monitor.record_gauge_by_name(
            MetricsConstants.SCHEDULER_TASKS_REGISTERED,
            1 if registered else 0,
            attributes,
        )
        self._monitor.record_gauge_by_name(
            MetricsConstants.SCHEDULER_TASK_INTERVAL,
            interval_seconds if registered else 0,
            attributes,
        )

    def set_alive_workers(self, worker_ips: set[str]) -> None:
        current_ips = worker_ips.copy()
        added_ips = current_ips - self._alive_worker_ips
        removed_ips = self._alive_worker_ips - current_ips
        for worker_ip in added_ips:
            self._monitor.record_gauge_by_name(
                MetricsConstants.SCHEDULER_WORKER_ALIVE,
                1,
                {"worker_ip": worker_ip},
            )
        for worker_ip in removed_ips:
            self._monitor.record_gauge_by_name(
                MetricsConstants.SCHEDULER_WORKER_ALIVE,
                0,
                {"worker_ip": worker_ip},
            )
        self._monitor.record_gauge_by_name(MetricsConstants.SCHEDULER_WORKERS_ALIVE, len(current_ips))
        self._alive_worker_ips = current_ips

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
        self._monitor.record_gauge_by_name(MetricsConstants.SCHEDULER_WORKER_CACHE_TTL, cache_ttl)
        if success:
            self._monitor.record_gauge_by_name(
                MetricsConstants.SCHEDULER_WORKER_CACHE_LAST_SUCCESS_TIMESTAMP,
                time.time(),
            )
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
