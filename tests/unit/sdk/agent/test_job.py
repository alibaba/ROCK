import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from rock.sdk.bench.job import Job, JobResult, JobStatus
from rock.sdk.bench.models.job.config import (
    HarborJobConfig,
    LocalDatasetConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RockEnvironmentConfig,
)
from rock.sdk.bench.models.trial.config import AgentConfig
from rock.sdk.bench.models.trial.result import ExceptionInfo, HarborTrialResult, VerifierResult


class TestJobStatus:
    def test_values(self):
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.CANCELLED == "cancelled"


class TestHarborTrialResult:
    def test_defaults(self):
        t = HarborTrialResult(task_name="fix-bug")
        assert t.task_name == "fix-bug"
        assert t.score == 0.0  # computed property from verifier_result
        assert t.status == "completed"  # computed property, no exception_info
        assert t.token_ids == []  # computed property
        assert t.duration_sec == 0.0  # computed property

    def test_with_verifier_result(self):
        t = HarborTrialResult(
            task_name="fix-bug",
            verifier_result=VerifierResult(rewards={"reward": 1.0}),
        )
        assert t.score == 1.0

    def test_failed_trial(self):
        t = HarborTrialResult(
            task_name="fix-bug",
            exception_info=ExceptionInfo(
                exception_type="TimeoutError",
                exception_message="agent timed out",
            ),
        )
        assert t.status == "failed"

    def test_from_harbor_json(self):
        data = {
            "trial_name": "trial-001",
            "task_name": "fix-dockerfile",
            "started_at": "2026-03-27T10:00:00Z",
            "finished_at": "2026-03-27T10:05:30Z",
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {"n_input_tokens": 15000, "n_output_tokens": 3000},
            "exception_info": None,
        }
        t = HarborTrialResult.from_harbor_json(data)
        assert t.task_name == "fix-dockerfile"
        assert t.trial_name == "trial-001"
        assert t.score == 1.0
        assert t.status == "completed"


class TestJobResult:
    def test_basic(self):
        r = JobResult(
            job_id="job-123",
            status=JobStatus.COMPLETED,
            trial_results=[
                HarborTrialResult(task_name="t1", verifier_result=VerifierResult(rewards={"reward": 1.0})),
                HarborTrialResult(task_name="t2", verifier_result=VerifierResult(rewards={"reward": 0.5})),
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
            trial_results=[
                HarborTrialResult(task_name="t1", verifier_result=VerifierResult(rewards={"reward": 1.0})),
                HarborTrialResult(
                    task_name="t2",
                    exception_info=ExceptionInfo(exception_type="Error", exception_message="err"),
                ),
            ],
            raw_output="",
            exit_code=0,
        )
        assert r.score == 0.5
        assert r.n_completed == 1
        assert r.n_failed == 1

    def test_empty_trials(self):
        r = JobResult(job_id="job-789", status=JobStatus.FAILED, trial_results=[], raw_output="error", exit_code=1)
        assert r.score == 0.0
        assert r.n_completed == 0
        assert r.n_failed == 0

    def test_labels_default_empty(self):
        r = JobResult(job_id="job-no-labels")
        assert r.labels == {}

    def test_labels_preserved(self):
        r = JobResult(
            job_id="job-labeled",
            labels={"step": "42", "env": "prod"},
            trial_results=[
                HarborTrialResult(task_name="t1", verifier_result=VerifierResult(rewards={"reward": 1.0})),
            ],
        )
        assert r.labels == {"step": "42", "env": "prod"}


def _make_mock_sandbox():
    """Create a mock Sandbox with all required async methods."""
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-123"
    sandbox._namespace = "test-ns"
    sandbox._experiment_id = "test-exp"
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.upload_by_path = AsyncMock(return_value=MagicMock(success=True))
    sandbox.fs = AsyncMock()
    sandbox.fs.upload_dir = AsyncMock()

    # execute for find command
    execute_result = MagicMock()
    execute_result.stdout = "/jobs/test-job/trials/trial-001/result.json"
    sandbox.execute = AsyncMock(return_value=execute_result)

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

    # read_file returns trial result.json content
    read_response = MagicMock()
    read_response.content = json.dumps(
        {
            "trial_name": "trial-001",
            "task_name": "t1",
            "started_at": "2026-03-27T10:00:00Z",
            "finished_at": "2026-03-27T10:05:00Z",
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {},
            "exception_info": None,
        }
    )
    sandbox.read_file = AsyncMock(return_value=read_response)

    return sandbox


class TestJob:
    def test_init_requires_jobconfig(self):
        config = HarborJobConfig(experiment_id="test-exp")
        job = Job(config)
        assert job._config == config

    def test_init_rejects_wrong_type(self):
        import pytest

        with pytest.raises(TypeError):
            Job("not a config")

    async def test_run_full_lifecycle(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(
                experiment_id="test-exp",
                job_name="test-job",
                agents=[AgentConfig(name="t2")],
                datasets=[RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="tb", version="2.0")],
            )
            job = Job(config)
            result = await job.run()

            assert result.status == JobStatus.COMPLETED
            assert len(result.trial_results) == 1
            assert result.trial_results[0].score == 1.0

            # Verify sandbox was started
            mock_sandbox.start.assert_called_once()

            # Verify harbor command was started via nohup
            mock_sandbox.start_nohup_process.assert_called_once()

    async def test_run_auto_stop_sandbox(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(
                job_name="test-job", experiment_id="test-exp", environment=RockEnvironmentConfig(auto_stop=True)
            )
            job = Job(config)
            await job.run()

            mock_sandbox.close.assert_called_once()

    async def test_run_does_not_stop_when_disabled(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(
                job_name="test-job", experiment_id="test-exp", environment=RockEnvironmentConfig(auto_stop=False)
            )
            job = Job(config)
            await job.run()

            mock_sandbox.close.assert_not_called()

    async def test_submit_starts_sandbox(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(job_name="test-job", experiment_id="test-exp")
            job = Job(config)
            await job.submit()

            mock_sandbox.start.assert_called_once()
            mock_sandbox.start_nohup_process.assert_called_once()

    async def test_wait_returns_result(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(job_name="test-job", experiment_id="test-exp")
            job = Job(config)
            await job.submit()
            result = await job.wait()

            assert isinstance(result, JobResult)
            assert result.status == JobStatus.COMPLETED


class TestBuildSessionEnv:
    def test_oss_vars_from_process_env_are_forwarded(self, monkeypatch):
        monkeypatch.setenv("OSS_ENDPOINT", "https://oss.example.com")
        monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-key")
        monkeypatch.setenv("HOME", "/root")

        job = Job(HarborJobConfig(job_name="test-job", experiment_id="test-exp"))
        env = job._build_session_env()

        assert env["OSS_ENDPOINT"] == "https://oss.example.com"
        assert env["OSS_ACCESS_KEY_ID"] == "test-key"
        assert "HOME" not in env

    def test_config_env_overrides_process_oss_vars(self, monkeypatch):
        monkeypatch.setenv("OSS_ENDPOINT", "https://oss.from.process.com")

        job = Job(
            HarborJobConfig(
                job_name="test-job",
                experiment_id="test-exp",
                environment=RockEnvironmentConfig(env={"OSS_ENDPOINT": "https://oss.from.config.com"}),
            )
        )
        env = job._build_session_env()

        assert env["OSS_ENDPOINT"] == "https://oss.from.config.com"

    def test_returns_none_when_both_empty(self, monkeypatch):
        for key in list(os.environ.keys()):
            if key.startswith("OSS"):
                monkeypatch.delenv(key)

        job = Job(HarborJobConfig(job_name="test-job", experiment_id="test-exp"))
        assert job._build_session_env() is None


class TestCancelKillsProcess:
    async def test_cancel_kills_process(self):
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.arun = AsyncMock(return_value=MagicMock(output="", exit_code=0))

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(job_name="test-job", experiment_id="test-exp")
            job = Job(config)
            await job.submit()
            await job.cancel()

            mock_sandbox.arun.assert_called_once()
            # Verify kill command was issued
            call_args = mock_sandbox.arun.call_args
            assert "kill" in str(call_args)


class TestGenerateDefaultJobName:
    """Tests for _generate_default_job_name method."""

    def test_custom_job_name_not_overwritten(self):
        """User-set job_name should not be overwritten."""
        config = HarborJobConfig(
            job_name="my-custom-job",
            experiment_id="test-exp",
            datasets=[RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="tb", version="2.0")],
        )
        job = Job(config)
        job._generate_default_job_name()

        assert job._config.job_name == "my-custom-job"

    def test_job_name_generated_with_dataset_and_single_task(self):
        """Default job_name should be generated with dataset name and single task."""
        config = HarborJobConfig(
            experiment_id="test-exp",
            datasets=[
                RegistryDatasetConfig(
                    registry=RemoteRegistryInfo(),
                    name="terminal-bench",
                    version="2.0",
                    task_names=["fix-bug"],
                )
            ],
        )
        job = Job(config)
        job._generate_default_job_name()

        # Should be: terminal-bench_fix-bug_{uuid}
        job_name = job._config.job_name
        parts = job_name.split("_")
        assert len(parts) == 3
        assert parts[0] == "terminal-bench"
        assert parts[1] == "fix-bug"
        assert len(parts[2]) == 8  # UUID is 8 chars

    def test_job_name_generated_with_dataset_multiple_tasks(self):
        """With multiple tasks, only dataset name and UUID should be used."""
        config = HarborJobConfig(
            experiment_id="test-exp",
            datasets=[
                RegistryDatasetConfig(
                    registry=RemoteRegistryInfo(),
                    name="terminal-bench",
                    version="2.0",
                    task_names=["task1", "task2"],
                )
            ],
        )
        job = Job(config)
        job._generate_default_job_name()

        # Should be: terminal-bench_{uuid}
        job_name = job._config.job_name
        parts = job_name.split("_")
        assert len(parts) == 2
        assert parts[0] == "terminal-bench"
        assert len(parts[1]) == 8  # UUID is 8 chars

    def test_job_name_generated_without_dataset(self):
        """Without dataset, only UUID should be used."""
        config = HarborJobConfig(experiment_id="test-exp")
        job = Job(config)
        job._generate_default_job_name()

        # Should be: {uuid}
        job_name = job._config.job_name
        assert len(job_name) == 8  # Only UUID

    def test_job_name_generated_with_dataset_no_name(self):
        """Dataset without name field should still work."""
        config = HarborJobConfig(
            experiment_id="test-exp",
            datasets=[LocalDatasetConfig(path=Path("/data/tasks"))],
        )
        job = Job(config)
        job._generate_default_job_name()

        # LocalDatasetConfig has no name, so only UUID
        job_name = job._config.job_name
        assert len(job_name) == 8  # Only UUID

    async def test_submit_generates_job_name(self):
        """Verify that submit() triggers the job_name generation."""
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = HarborJobConfig(
                experiment_id="test-exp",
                datasets=[
                    RegistryDatasetConfig(
                        registry=RemoteRegistryInfo(),
                        name="my-dataset",
                        task_names=["my-task"],
                    )
                ],
            )
            job = Job(config)

            # G3: job_name is auto-generated by the pydantic validator at construction time.
            assert config.job_name is not None
            assert config.job_name.startswith("my-dataset_my-task_")

            await job.submit()

            # submit() must preserve the (already-generated) job_name
            assert job._config.job_name is not None
            assert job._config.job_name.startswith("my-dataset_my-task_")
