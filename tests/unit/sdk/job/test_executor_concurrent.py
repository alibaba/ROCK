from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rock.sdk.job.executor import JobExecutor
from rock.sdk.job.planner import PlannedJob
from rock.sdk.job.result import TrialResult


class DummyTrial:
    def __init__(self, name: str):
        self._config = SimpleNamespace(job_name=name, labels={"rock_task_id": name})


class TestJobExecutorRunTrials:
    async def test_run_trials_respects_max_concurrent_for_submit_and_wait_lifecycle(self):
        executor = JobExecutor(max_concurrent=2)
        active = 0
        max_active = 0

        async def fake_submit(trial):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            return SimpleNamespace(trial=trial)

        async def fake_wait(client):
            nonlocal active
            await asyncio.sleep(0.01)
            active -= 1
            return TrialResult(task_name=client.trial._config.job_name)

        executor._do_submit = fake_submit
        executor._do_wait = fake_wait

        results = await executor.run_trials([DummyTrial(f"t{i}") for i in range(5)])

        assert [r.task_name for r in results] == ["t0", "t1", "t2", "t3", "t4"]
        assert max_active <= 2

    async def test_run_trials_converts_lifecycle_exception_to_failed_trial_result(self):
        executor = JobExecutor(max_concurrent=1)
        trial = DummyTrial("task-1")

        async def fake_submit(_trial):
            raise RuntimeError("sandbox boom")

        executor._do_submit = fake_submit

        results = await executor.run_trials([trial])

        assert len(results) == 1
        assert results[0].task_name == "task-1"
        assert results[0].status == "failed"
        assert results[0].exception_info.exception_type == "RuntimeError"
        assert "sandbox boom" in results[0].exception_info.exception_message


class TestJobExecutorRunJob:
    async def test_run_job_calls_done_callback_when_wait_fails_after_start(self):
        executor = JobExecutor()
        trial = DummyTrial("task-1")
        client = SimpleNamespace(trial=trial, sandbox=SimpleNamespace(sandbox_id="sb-1"), session="s", pid=1)
        done_results = []

        async def fake_submit(_trial):
            return client

        async def fake_wait(_client):
            raise RuntimeError("No trial results found")

        class Callbacks:
            def on_started(self, _client):
                pass

            def on_done(self, _client, result):
                done_results.append(result)

        executor._do_submit = fake_submit
        executor._do_wait = fake_wait

        result = await executor.run_job(
            PlannedJob(task_id="task-1", job_name="job-1", config=SimpleNamespace(), trial=trial),
            callbacks=Callbacks(),
        )

        assert result.status == "failed"
        assert result.exception_info.exception_message == "No trial results found"
        assert len(done_results) == 1
        assert done_results[0].status == "failed"
