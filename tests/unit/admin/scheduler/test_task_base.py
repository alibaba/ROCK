from unittest.mock import AsyncMock, patch

import pytest

from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask


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
        await task.run(["10.0.0.1"])

    assert observed_timeouts == [90]
    report = (tmp_path / "test_run_report.json").read_text()
    assert '"failed_count": 1' in report
    assert "worker timed out" in report
