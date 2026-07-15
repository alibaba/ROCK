import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import rock.admin.scheduler.metrics as metrics_module
from rock.admin.scheduler.scheduler import SchedulerThread, TaskScheduler, WorkerIPCache
from rock.admin.scheduler.task_base import TaskRunOutcome, TaskRunReport
from rock.config import SchedulerConfig, TaskConfig


def _report(task_type: str = "test", outcome: TaskRunOutcome = TaskRunOutcome.NO_WORKERS) -> TaskRunReport:
    assert outcome == TaskRunOutcome.NO_WORKERS
    return TaskRunReport(
        task_type=task_type,
        timestamp="2026-07-14T12:00:00",
        duration_ms=1.0,
        worker_results=[],
    )


def test_scheduler_objects_use_shared_noop_metrics_by_default():
    scheduler_config = SchedulerConfig(enabled=True)

    assert WorkerIPCache()._metrics is metrics_module.NOOP_SCHEDULER_METRICS
    assert TaskScheduler(scheduler_config)._metrics is metrics_module.NOOP_SCHEDULER_METRICS
    assert SchedulerThread(scheduler_config).metrics is metrics_module.NOOP_SCHEDULER_METRICS


def test_worker_cache_successful_refresh_updates_metrics(monkeypatch):
    metrics = MagicMock()
    cache = WorkerIPCache(cache_ttl=120, metrics=metrics)
    monkeypatch.setattr(cache, "_fetch_worker_ips_from_ray", lambda: {"10.0.0.1", "10.0.0.2"})

    assert cache.refresh() == {"10.0.0.1", "10.0.0.2"}

    metrics.record_worker_cache_refresh.assert_called_once_with(
        success=True,
        cache_ttl=120,
        worker_ips={"10.0.0.1", "10.0.0.2"},
    )


def test_worker_cache_fetches_worker_ips_as_a_deduplicated_set(monkeypatch):
    cache = WorkerIPCache()
    monkeypatch.setattr(
        "rock.admin.scheduler.scheduler.ray.nodes",
        lambda: [
            {"Alive": True, "Resources": {"CPU": 1}, "NodeManagerAddress": "10.0.0.1"},
            {"Alive": True, "Resources": {"CPU": 2}, "NodeManagerAddress": "10.0.0.1"},
            {"Alive": False, "Resources": {"CPU": 1}, "NodeManagerAddress": "10.0.0.2"},
        ],
    )

    worker_ips = cache._fetch_worker_ips_from_ray()

    assert worker_ips == {"10.0.0.1"}
    assert isinstance(worker_ips, set)


def test_worker_cache_failed_refresh_preserves_cached_ips_and_records_failure(monkeypatch):
    metrics = MagicMock()
    cache = WorkerIPCache(cache_ttl=120, metrics=metrics)
    cache._cached_ips = {"10.0.0.1"}
    cache._cache_time = 123.0

    def fail():
        raise RuntimeError("ray unavailable")

    monkeypatch.setattr(cache, "_fetch_worker_ips_from_ray", fail)

    assert cache.refresh() == {"10.0.0.1"}
    assert cache._cache_time == 123.0
    metrics.record_worker_cache_refresh.assert_called_once_with(success=False, cache_ttl=120)


def test_worker_cache_ttl_hit_does_not_refresh(monkeypatch):
    metrics = MagicMock()
    cache = WorkerIPCache(cache_ttl=120, metrics=metrics)
    cache._cached_ips = {"10.0.0.1"}
    cache._cache_time = 100.0
    fetch = MagicMock(return_value={"10.0.0.2"})
    monkeypatch.setattr(cache, "_fetch_worker_ips_from_ray", fetch)
    monkeypatch.setattr("rock.admin.scheduler.scheduler.time.time", lambda: 150.0)

    assert cache.get_alive_workers() == {"10.0.0.1"}
    fetch.assert_not_called()
    metrics.record_worker_cache_refresh.assert_not_called()


def test_worker_cache_successful_empty_result_still_obeys_ttl(monkeypatch):
    cache = WorkerIPCache(cache_ttl=120)
    fetch = MagicMock(return_value=set())
    monkeypatch.setattr(cache, "_fetch_worker_ips_from_ray", fetch)
    now = [100.0]
    monkeypatch.setattr("rock.admin.scheduler.scheduler.time.time", lambda: now[0])

    assert cache.refresh() == set()
    now[0] = 150.0
    assert cache.get_alive_workers() == set()

    fetch.assert_called_once()


def test_scheduler_thread_alive_workers_are_a_snapshot_without_refresh():
    cache = WorkerIPCache(cache_ttl=0)
    cache._cached_ips = {"10.0.0.1"}
    cache.refresh = MagicMock(return_value=cache._cached_ips)
    task_scheduler = TaskScheduler(SchedulerConfig(enabled=True))
    task_scheduler._worker_cache = cache
    scheduler_thread = SchedulerThread(SchedulerConfig(enabled=True))
    scheduler_thread._task_scheduler = task_scheduler

    worker_ips = scheduler_thread.get_alive_workers()
    worker_ips.add("10.0.0.2")

    cache.refresh.assert_not_called()
    assert cache._cached_ips == {"10.0.0.1"}


@pytest.mark.asyncio
async def test_run_task_records_no_workers_report():
    metrics = MagicMock()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)
    scheduler._worker_cache = MagicMock()
    scheduler._worker_cache.get_alive_workers.return_value = set()
    report = _report()
    task = SimpleNamespace(type="test", run=AsyncMock(return_value=report))

    await scheduler._run_task(task)

    task.run.assert_awaited_once_with(set())
    metrics.record_task_report.assert_called_once_with(report)


@pytest.mark.asyncio
async def test_reload_flushes_updated_task_metrics():
    metrics = MagicMock()
    metrics.flush_and_wait = AsyncMock()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)
    scheduler._worker_cache = MagicMock()
    scheduler._rebuild_tasks = AsyncMock()
    updated = SchedulerConfig(enabled=True, worker_cache_ttl=900)

    await scheduler._reload_scheduler_config(updated)

    assert scheduler._worker_cache.cache_ttl == 900
    scheduler._rebuild_tasks.assert_awaited_once()
    metrics.flush_and_wait.assert_awaited_once()


def test_install_task_records_registration(monkeypatch):
    metrics = MagicMock()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)
    scheduler._scheduler = MagicMock()
    task = SimpleNamespace(type="docker_health", interval_seconds=60)
    monkeypatch.setattr("rock.admin.scheduler.task_factory.TaskFactory.create_task", lambda config: task)
    config = TaskConfig(task_class="tasks.DockerHealthTask", interval_seconds=60)

    scheduler._install_task(config)

    scheduler._scheduler.add_job.assert_called_once()
    metrics.set_registered_task.assert_called_once_with("docker_health", 60, True)


def test_install_task_failure_does_not_register_task(monkeypatch):
    metrics = MagicMock()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)
    scheduler._scheduler = MagicMock()

    def fail(config):
        raise ValueError("invalid task")

    monkeypatch.setattr("rock.admin.scheduler.task_factory.TaskFactory.create_task", fail)
    config = TaskConfig(task_class="tasks.InvalidTask", interval_seconds=60)

    scheduler._install_task(config)

    metrics.set_registered_task.assert_not_called()


@pytest.mark.asyncio
async def test_uninstall_marks_task_unregistered_even_when_cleanup_fails():
    metrics = MagicMock()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)
    scheduler._scheduler = MagicMock()
    scheduler._worker_cache = MagicMock()
    scheduler._worker_cache.get_alive_workers.return_value = {"10.0.0.1"}
    task = SimpleNamespace(
        type="docker_health",
        interval_seconds=60,
        cleanup=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    scheduler._tasks_by_class["tasks.DockerHealthTask"] = task

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await scheduler._uninstall_task("tasks.DockerHealthTask")

    metrics.set_registered_task.assert_called_once_with("docker_health", 60, False)


def test_nacos_processing_failure_does_not_require_control_event_metrics():
    class MetricsWithoutControlEvents:
        pass

    metrics = MetricsWithoutControlEvents()
    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=metrics)

    class RunningLoop:
        @staticmethod
        def is_running():
            return True

        @staticmethod
        def call_soon_threadsafe(callback, *args):
            callback(*args)

    scheduler._event_loop = RunningLoop()

    scheduler._on_nacos_config_changed({"content": "scheduler: [invalid"})


@pytest.mark.asyncio
async def test_scheduler_lifecycle_flushes_started_and_stopped_state(monkeypatch):
    events = []

    class FakeMetrics:
        def set_scheduler_up(self, up):
            events.append(("up", up))

        async def flush_and_wait(self):
            events.append(("flush",))

    class FakeAPScheduler:
        def __init__(self, timezone):
            self.running = False

        def start(self):
            self.running = True

        def shutdown(self, wait):
            events.append(("apscheduler_shutdown", wait))
            self.running = False

    scheduler = TaskScheduler(SchedulerConfig(enabled=True), metrics=FakeMetrics())
    worker_cache = MagicMock()
    scheduler._init_worker_cache = lambda: setattr(scheduler, "_worker_cache", worker_cache)
    scheduler._rebuild_tasks = AsyncMock()
    monkeypatch.setattr("rock.admin.scheduler.scheduler.AsyncIOScheduler", FakeAPScheduler)

    scheduler.stop()
    await asyncio.wait_for(scheduler.run(), timeout=1)

    assert worker_cache.refresh.call_count == 1
    assert events == [
        ("up", True),
        ("flush",),
        ("apscheduler_shutdown", False),
        ("up", False),
        ("flush",),
    ]
