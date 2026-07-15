import json
from unittest.mock import AsyncMock, patch

import pytest

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask, TaskStatusEnum


class _Task(BaseTask):
    def __init__(self):
        super().__init__(type="test", interval_seconds=60)

    async def run_action(self, runtime):
        return {}


@pytest.mark.asyncio
async def test_run_times_out_each_worker_after_90_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()
    task.run_on_worker = AsyncMock()
    observed_timeouts = []

    async def record_timeout(awaitable, timeout):
        observed_timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError("worker timed out")

    with patch("rock.admin.scheduler.task_base.asyncio.wait_for", side_effect=record_timeout):
        report = await task.run({"10.0.0.1"})

    assert observed_timeouts == [90]
    assert report.outcome.value == "failed"
    assert report.timeout_count == 1
    report = (tmp_path / "test_run_report.json").read_text()
    assert '"failed_count": 1' in report
    assert '"timeout_count": 1' in report
    assert "worker timed out" in report


@pytest.mark.asyncio
async def test_run_returns_no_workers_report(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()

    report = await task.run(set())

    assert report.outcome.value == "no_workers"
    assert report.total_count == 0
    assert report.duration_ms >= 0
    persisted = json.loads((tmp_path / "test_run_report.json").read_text())
    assert persisted["total"] == 0
    assert persisted["outcome"] == "no_workers"


@pytest.mark.asyncio
async def test_run_classifies_worker_results_and_keeps_compatible_report_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()

    async def run_on_worker(ip):
        if ip == "success":
            return {"status": TaskStatusEnum.SUCCESS}
        if ip == "started":
            return {"status": TaskStatusEnum.RUNNING, "pid": 123}
        if ip == "skipped":
            return None
        raise ValueError("worker failed")

    task.run_on_worker = run_on_worker

    report = await task.run({"success", "started", "skipped", "failed"})

    assert report.outcome.value == "partial"
    assert report.total_count == 4
    assert report.success_count == 3
    assert report.started_count == 1
    assert report.skipped_count == 1
    assert report.failed_count == 1
    assert report.timeout_count == 0
    assert {result.worker_ip: result.outcome.value for result in report.worker_results} == {
        "success": "success",
        "started": "started",
        "skipped": "skipped",
        "failed": "failed",
    }
    assert not hasattr(report.worker_results[0], "duration_ms")
    assert not hasattr(report.worker_results[0], "__dict__")
    assert not hasattr(report, "__dict__")
    assert not hasattr(report.worker_results[0], "effect_data")

    persisted = json.loads((tmp_path / "test_run_report.json").read_text())
    assert persisted["total"] == 4
    assert persisted["success_count"] == 3
    assert persisted["failed_count"] == 1
    assert set(persisted["success_ips"]) == {"success", "started", "skipped"}
    assert persisted["failed_details"][0]["ip"] == "failed"
    failed_result = next(result for result in persisted["worker_results"] if result["worker_ip"] == "failed")
    assert failed_result["error_type"] == "ValueError"
    assert "effect_data" not in persisted["worker_results"][0]


@pytest.mark.asyncio
async def test_run_treats_failed_task_result_as_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()
    task.run_on_worker = AsyncMock(return_value={"status": TaskStatusEnum.FAILED, "error": "bad result"})

    report = await task.run({"10.0.0.1"})

    assert report.outcome.value == "failed"
    assert report.failed_count == 1
    assert report.worker_results[0].error_type == "TaskResultFailed"


@pytest.mark.asyncio
async def test_run_report_discards_unneeded_task_result_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()
    task.run_on_worker = AsyncMock(
        return_value={
            "status": TaskStatusEnum.SUCCESS,
            "archived": 3,
            "removed_count": 99,
            "output_head": "x" * 100_000,
            "details": {"large": "y" * 100_000},
            "unexpected": "not retained",
        }
    )

    report = await task.run({"10.0.0.1"})

    assert not hasattr(report.worker_results[0], "effect_data")
    persisted = json.loads((tmp_path / "test_run_report.json").read_text())
    persisted_result = persisted["worker_results"][0]
    assert "effect_data" not in persisted_result
    assert "result" not in persisted_result
    assert "archived" not in persisted_result
    assert "removed_count" not in persisted_result
    assert "output_head" not in persisted_result
    assert "details" not in persisted_result


@pytest.mark.asyncio
async def test_run_caps_error_detail_at_4096_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()
    task.run_on_worker = AsyncMock(
        return_value={
            "status": TaskStatusEnum.FAILED,
            "error": "错误" * 10_000,
            "details": "y" * 100_000,
        }
    )

    report = await task.run({"10.0.0.1"})

    error = report.worker_results[0].error
    assert error is not None
    assert len(error.encode("utf-8")) <= 4096
    assert error.endswith("...[truncated]")


@pytest.mark.asyncio
async def test_run_reports_all_skipped_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_SCHEDULER_STATUS_DIR", str(tmp_path))
    task = _Task()
    task.run_on_worker = AsyncMock(return_value=None)

    report = await task.run({"10.0.0.1", "10.0.0.2"})

    assert report.outcome.value == "skipped"
    assert report.success_count == 2
    assert report.skipped_count == 2
