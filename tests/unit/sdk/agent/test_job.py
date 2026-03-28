import json
from unittest.mock import AsyncMock, MagicMock

from rock.sdk.agent.job import Job, JobResult, JobStatus, TrialResult
from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig


class TestJobStatus:
    def test_values(self):
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.CANCELLED == "cancelled"


class TestTrialResult:
    def test_defaults(self):
        t = TrialResult(task_name="fix-bug", score=1.0)
        assert t.task_name == "fix-bug"
        assert t.score == 1.0
        assert t.status == JobStatus.COMPLETED
        assert t.rewards == {}
        assert t.trajectory_path is None
        assert t.token_ids == []
        assert t.duration_sec == 0.0
        assert t.error is None

    def test_failed_trial(self):
        t = TrialResult(
            task_name="fix-bug",
            score=0.0,
            status=JobStatus.FAILED,
            error="TimeoutError",
            duration_sec=300.0,
        )
        assert t.status == JobStatus.FAILED
        assert t.error == "TimeoutError"


class TestJobResult:
    def test_basic(self):
        r = JobResult(
            job_id="job-123",
            status=JobStatus.COMPLETED,
            trials=[
                TrialResult(task_name="t1", score=1.0),
                TrialResult(task_name="t2", score=0.5),
            ],
            raw_output="",
            exit_code=0,
        )
        assert r.job_id == "job-123"
        assert r.score == 0.75
        assert r.n_completed == 2
        assert r.n_failed == 0

    def test_score_with_failed_trials(self):
        r = JobResult(
            job_id="job-456",
            status=JobStatus.COMPLETED,
            trials=[
                TrialResult(task_name="t1", score=1.0),
                TrialResult(task_name="t2", score=0.0, status=JobStatus.FAILED, error="err"),
            ],
            raw_output="",
            exit_code=0,
        )
        assert r.score == 0.5
        assert r.n_completed == 1
        assert r.n_failed == 1

    def test_empty_trials(self):
        r = JobResult(job_id="job-789", status=JobStatus.FAILED, trials=[], raw_output="error", exit_code=1)
        assert r.score == 0.0
        assert r.n_completed == 0
        assert r.n_failed == 0


class TestParseHarborResult:
    """Test parsing Harbor result.json into JobResult."""

    def test_parse_result_json(self):
        harbor_result = {
            "job_name": "my-job",
            "stats": {"n_trials": 2, "n_errors": 0, "evals": {"tb": {"metrics": {"mean": 0.72}}}},
            "trial_results": [
                {
                    "trial_name": "trial-001",
                    "task_name": "fix-dockerfile",
                    "started_at": "2026-03-27T10:00:00Z",
                    "finished_at": "2026-03-27T10:05:30Z",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "agent_result": {"n_input_tokens": 15000, "n_output_tokens": 3000},
                    "exception_info": None,
                },
                {
                    "trial_name": "trial-002",
                    "task_name": "fix-syntax",
                    "started_at": "2026-03-27T10:06:00Z",
                    "finished_at": "2026-03-27T10:08:00Z",
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "agent_result": {"n_input_tokens": 8000, "n_output_tokens": 1500},
                    "exception_info": None,
                },
            ],
        }
        result = JobResult.from_harbor_result(json.dumps(harbor_result), job_id="test-job")
        assert result.job_id == "test-job"
        assert result.status == JobStatus.COMPLETED
        assert len(result.trials) == 2
        assert result.trials[0].task_name == "fix-dockerfile"
        assert result.trials[0].score == 1.0
        assert result.trials[0].rewards == {"reward": 1.0}
        assert result.trials[1].score == 0.0

    def test_parse_result_with_error(self):
        harbor_result = {
            "job_name": "my-job",
            "stats": {"n_trials": 1, "n_errors": 1},
            "trial_results": [
                {
                    "trial_name": "trial-001",
                    "task_name": "fix-bug",
                    "started_at": "2026-03-27T10:00:00Z",
                    "finished_at": "2026-03-27T10:01:00Z",
                    "verifier_result": None,
                    "agent_result": None,
                    "exception_info": "AgentTimeoutError: agent timed out after 300s",
                },
            ],
        }
        result = JobResult.from_harbor_result(json.dumps(harbor_result), job_id="err-job")
        assert result.trials[0].status == JobStatus.FAILED
        assert result.trials[0].error == "AgentTimeoutError: agent timed out after 300s"
        assert result.trials[0].score == 0.0


def _make_mock_sandbox():
    """Create a mock Sandbox with all required async methods."""
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-123"
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file = AsyncMock()

    # Default arun: returns successful observation
    obs = MagicMock()
    obs.output = ""
    obs.exit_code = 0
    obs.failure_reason = ""
    sandbox.arun = AsyncMock(return_value=obs)

    # start_nohup_process returns (pid, None) — success
    sandbox.start_nohup_process = AsyncMock(return_value=(12345, None))

    # wait_for_process_completion returns (True, "done")
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))

    # handle_nohup_output returns observation
    nohup_obs = MagicMock()
    nohup_obs.output = "harbor completed"
    nohup_obs.exit_code = 0
    nohup_obs.failure_reason = ""
    sandbox.handle_nohup_output = AsyncMock(return_value=nohup_obs)

    # read_file returns result.json content
    read_response = MagicMock()
    read_response.content = json.dumps(
        {
            "job_name": "test",
            "stats": {"n_trials": 1, "n_errors": 0},
            "trial_results": [
                {
                    "trial_name": "trial-001",
                    "task_name": "t1",
                    "started_at": "2026-03-27T10:00:00Z",
                    "finished_at": "2026-03-27T10:05:00Z",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "agent_result": {},
                    "exception_info": None,
                }
            ],
        }
    )
    sandbox.read_file = AsyncMock(return_value=read_response)

    return sandbox


class TestJobRun:
    async def test_run_full_lifecycle(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(
            auto_start_sandbox=False,
            agents=[AgentConfig(name="t2")],
            datasets=[DatasetConfig(name="tb", version="2.0")],
        )
        job = Job(config, sandbox=sandbox)
        result = await job.run()

        assert result.status == JobStatus.COMPLETED
        assert len(result.trials) == 1
        assert result.trials[0].score == 1.0

        # Verify config was uploaded
        sandbox.write_file.assert_called_once()

        # Verify harbor command was started via nohup
        sandbox.start_nohup_process.assert_called_once()
        cmd = sandbox.start_nohup_process.call_args[1]["cmd"]
        assert "harbor" in cmd
        assert ".yaml" in cmd or ".yml" in cmd

    async def test_run_with_setup_commands(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(
            auto_start_sandbox=False,
            setup_commands=["pip install harbor --quiet", "echo ready"],
        )
        job = Job(config, sandbox=sandbox)
        await job.run()

        # arun should have been called for each setup command
        arun_calls = sandbox.arun.call_args_list
        setup_cmds = [call[1].get("cmd", call[0][0] if call[0] else "") for call in arun_calls]
        assert "pip install harbor --quiet" in setup_cmds
        assert "echo ready" in setup_cmds

    async def test_run_auto_start_stop_sandbox(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=True, auto_stop_sandbox=True)
        job = Job(config, sandbox=sandbox)
        await job.run()

        sandbox.start.assert_called_once()
        sandbox.close.assert_called_once()

    async def test_run_skips_start_stop_when_disabled(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False, auto_stop_sandbox=False)
        job = Job(config, sandbox=sandbox)
        await job.run()

        sandbox.start.assert_not_called()
        sandbox.close.assert_not_called()


class TestJobSubmitWait:
    async def test_submit_returns_job_id(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False)
        job = Job(config, sandbox=sandbox)
        job_id = await job.submit()

        assert job_id is not None
        assert isinstance(job_id, str)
        sandbox.start_nohup_process.assert_called_once()

    async def test_wait_returns_result(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False)
        job = Job(config, sandbox=sandbox)
        job_id = await job.submit()
        result = await job.wait(job_id)

        assert isinstance(result, JobResult)
        assert result.status == JobStatus.COMPLETED
