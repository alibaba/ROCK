import asyncio
import threading
from collections import defaultdict

import pytest

import rock.admin.scheduler.metrics as metrics_module
from rock.admin.scheduler.metrics import SchedulerMetrics
from rock.admin.scheduler.task_base import (
    TaskRunReport,
    WorkerRunOutcome,
    WorkerRunResult,
)


class FakeMonitor:
    def __init__(self, force_flush_result=True):
        self.counters = []
        self.gauges = []
        self.force_flush_result = force_flush_result
        self.force_flush_calls = 0

    def record_counter_by_name(self, name, value=1, attributes=None):
        self.counters.append((name, value, attributes or {}))

    def record_gauge_by_name(self, name, value, attributes=None):
        self.gauges.append((name, value, attributes or {}))

    def force_flush(self, timeout_millis=10_000):
        self.force_flush_calls += 1
        return self.force_flush_result


def _report(task_type="file_cleanup", worker_results=None, duration_ms=1234.5):
    return TaskRunReport(
        task_type=task_type,
        timestamp="2026-07-14T12:00:00",
        duration_ms=duration_ms,
        worker_results=worker_results
        or [WorkerRunResult(worker_ip="10.0.0.1", outcome=WorkerRunOutcome.SUCCESS)],
    )


@pytest.mark.asyncio
async def test_noop_scheduler_metrics_accepts_all_scheduler_events_without_scheduling_flushes():
    noop = metrics_module.NOOP_SCHEDULER_METRICS
    tasks_before = set(asyncio.all_tasks())

    noop.set_scheduler_up(True)
    noop.set_registered_task("file_cleanup", 60, True)
    noop.record_worker_cache_refresh(success=True, cache_ttl=60, worker_ips={"10.0.0.1"})
    noop.record_task_report(_report())
    await noop.flush_and_wait()

    assert isinstance(noop, metrics_module.NoopSchedulerMetrics)
    assert set(asyncio.all_tasks()) == tasks_before
    assert not hasattr(noop, "_flush_task")


def test_scheduler_metrics_implementations_explicitly_inherit_recorder():
    recorder = metrics_module.SchedulerMetricsRecorder

    assert recorder in metrics_module.SchedulerMetrics.__mro__
    assert recorder in metrics_module.NoopSchedulerMetrics.__mro__


def test_incomplete_scheduler_metrics_implementation_cannot_be_instantiated():
    class IncompleteSchedulerMetrics(metrics_module.SchedulerMetricsRecorder):
        pass

    with pytest.raises(TypeError, match="abstract"):
        IncompleteSchedulerMetrics()


@pytest.mark.asyncio
async def test_success_only_task_report_does_not_record_or_flush_metrics():
    monitor = FakeMonitor()
    metrics = SchedulerMetrics(monitor)
    report = TaskRunReport(
        task_type="test",
        timestamp="2026-07-14T12:00:00",
        duration_ms=1.0,
        worker_results=[
            WorkerRunResult(worker_ip="10.0.0.1", outcome=WorkerRunOutcome.SUCCESS),
            WorkerRunResult(worker_ip="10.0.0.2", outcome=WorkerRunOutcome.STARTED),
            WorkerRunResult(worker_ip="10.0.0.3", outcome=WorkerRunOutcome.SKIPPED),
        ],
    )

    metrics.record_task_report(report)
    await asyncio.sleep(0)

    assert monitor.counters == []
    assert monitor.gauges == []
    assert monitor.force_flush_calls == 0


@pytest.mark.asyncio
async def test_record_task_report_only_exports_failed_worker_ips_with_bounded_labels(monkeypatch):
    monitor = FakeMonitor()
    metrics = SchedulerMetrics(monitor)
    monkeypatch.setattr("rock.admin.scheduler.metrics.time.time", lambda: 123.0)
    report = _report(
        worker_results=[
            WorkerRunResult(worker_ip="10.0.0.1", outcome=WorkerRunOutcome.SUCCESS),
            WorkerRunResult(
                worker_ip="10.0.0.2",
                outcome=WorkerRunOutcome.TIMEOUT,
                error_type="TimeoutError",
                error="timed out",
            ),
            WorkerRunResult(
                worker_ip="10.0.0.3",
                outcome=WorkerRunOutcome.FAILED,
                error_type="ConnectionError",
                error="connection failed",
            ),
        ]
    )

    metrics.record_task_report(report)
    await metrics.flush_and_wait()

    assert monitor.counters == [
        (
            "scheduler.worker.failures.total",
            1,
            {"task_type": "file_cleanup", "worker_ip": "10.0.0.2"},
        ),
        (
            "scheduler.worker.failures.total",
            1,
            {"task_type": "file_cleanup", "worker_ip": "10.0.0.3"},
        ),
    ]
    assert monitor.gauges == [
        (
            "scheduler.worker.last_failure.timestamp",
            123.0,
            {"task_type": "file_cleanup", "worker_ip": "10.0.0.2"},
        ),
        (
            "scheduler.worker.last_failure.timestamp",
            123.0,
            {"task_type": "file_cleanup", "worker_ip": "10.0.0.3"},
        ),
    ]
    assert monitor.force_flush_calls == 1


def test_set_alive_workers_marks_removed_ips_dead():
    monitor = FakeMonitor()
    metrics = SchedulerMetrics(monitor)

    metrics.set_alive_workers({"10.0.0.1", "10.0.0.2"})
    metrics.set_alive_workers({"10.0.0.2", "10.0.0.3"})

    per_ip_values = defaultdict(list)
    for name, value, attrs in monitor.gauges:
        if name == "scheduler.worker.alive":
            per_ip_values[attrs["worker_ip"]].append(value)
    assert per_ip_values == {
        "10.0.0.1": [1, 0],
        "10.0.0.2": [1],
        "10.0.0.3": [1],
    }
    assert ("scheduler.workers.alive", 2, {}) in monitor.gauges


@pytest.mark.asyncio
async def test_failed_worker_cache_refresh_preserves_alive_ips_and_flushes():
    monitor = FakeMonitor()
    metrics = SchedulerMetrics(monitor)
    metrics.set_alive_workers({"10.0.0.1"})
    monitor.gauges.clear()

    metrics.record_worker_cache_refresh(success=False, cache_ttl=3600)
    await metrics.flush_and_wait()

    assert not any(name == "scheduler.worker.alive" for name, _, _ in monitor.gauges)
    assert ("scheduler.worker_cache.refresh.total", 1, {"outcome": "failure"}) in monitor.counters
    assert ("scheduler.worker_cache.ttl", 3600, {}) in monitor.gauges
    assert monitor.force_flush_calls == 1


@pytest.mark.asyncio
async def test_successful_worker_cache_refresh_updates_timestamp_and_ips(monkeypatch):
    monitor = FakeMonitor()
    metrics = SchedulerMetrics(monitor)
    monkeypatch.setattr("rock.admin.scheduler.metrics.time.time", lambda: 123.0)

    metrics.record_worker_cache_refresh(success=True, cache_ttl=60, worker_ips={"10.0.0.1"})
    await metrics.flush_and_wait()

    assert ("scheduler.worker_cache.last_success.timestamp", 123.0, {}) in monitor.gauges
    assert ("scheduler.worker.alive", 1, {"worker_ip": "10.0.0.1"}) in monitor.gauges


@pytest.mark.asyncio
async def test_concurrent_flush_requests_are_coalesced_and_never_overlap():
    started = threading.Event()
    release = threading.Event()

    class BlockingMonitor(FakeMonitor):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.flush_thread_ids = []

        def force_flush(self, timeout_millis=10_000):
            self.force_flush_calls += 1
            self.flush_thread_ids.append(threading.get_ident())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            started.set()
            release.wait(timeout=2)
            self.active -= 1
            return True

    monitor = BlockingMonitor()
    metrics = SchedulerMetrics(monitor)
    event_loop_thread_id = threading.get_ident()

    metrics.request_flush()
    await asyncio.to_thread(started.wait, 2)
    metrics.request_flush()
    metrics.request_flush()
    release.set()
    await metrics.flush_and_wait()

    assert monitor.max_active == 1
    assert monitor.force_flush_calls == 2
    assert all(thread_id != event_loop_thread_id for thread_id in monitor.flush_thread_ids)


@pytest.mark.asyncio
async def test_flush_failure_does_not_record_control_event_or_raise():
    monitor = FakeMonitor(force_flush_result=False)
    metrics = SchedulerMetrics(monitor)

    metrics.request_flush()
    await metrics.flush_and_wait()

    assert not hasattr(metrics, "record_control_event")
    assert all(name != "scheduler.control_events.total" for name, _, _ in monitor.counters)
