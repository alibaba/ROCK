"""Tests for rock.sdk.job.executor — JobExecutor, TrialClient, JobClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import rock.sdk.job.trial.bash  # register BashJobConfig -> BashTrial  # noqa: F401
from rock.sdk.bench.constants import USER_DEFINED_LOGS
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.executor import JobClient, JobExecutor, TrialClient
from rock.sdk.job.operator import ScatterOperator


def _make_mock_sandbox():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-test"
    sandbox._namespace = None
    sandbox._experiment_id = None
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))

    # fs.upload_dir for _upload_files — returns success obs
    upload_obs = MagicMock()
    upload_obs.exit_code = 0
    sandbox.fs = AsyncMock()
    sandbox.fs.upload_dir = AsyncMock(return_value=upload_obs)

    sandbox.start_nohup_process = AsyncMock(return_value=(12345, None))
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))

    nohup_obs = MagicMock()
    nohup_obs.output = "hello output"
    nohup_obs.exit_code = 0
    sandbox.handle_nohup_output = AsyncMock(return_value=nohup_obs)
    return sandbox


# ---------------------------------------------------------------------------
# run() — full lifecycle
# ---------------------------------------------------------------------------


class TestJobExecutorRun:
    async def test_run_bash_single_trial_success(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            results = await executor.run(ScatterOperator(size=1), config)

        assert len(results) == 1
        assert results[0].exception_info is None
        assert mock_sandbox.start.call_count == 1
        assert mock_sandbox.start_nohup_process.call_count == 1

    async def test_run_empty_operator_returns_empty_list(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            results = await executor.run(ScatterOperator(size=0), config)

        assert results == []
        assert mock_sandbox.start.call_count == 0

    async def test_run_scatter_size_three_runs_three_trials(self):
        # Each Sandbox(...) call returns a fresh mock so we can verify 3 starts.
        mocks = [_make_mock_sandbox() for _ in range(3)]
        with patch("rock.sdk.job.executor.Sandbox", side_effect=mocks):
            config = BashJobConfig(script="echo hi", job_name="triple")
            executor = JobExecutor()
            results = await executor.run(ScatterOperator(size=3), config)

        assert len(results) == 3
        for mock_sandbox in mocks:
            assert mock_sandbox.start.call_count == 1
            assert mock_sandbox.start_nohup_process.call_count == 1


# ---------------------------------------------------------------------------
# submit() / wait() separately
# ---------------------------------------------------------------------------


class TestJobExecutorSubmit:
    async def test_submit_returns_job_client(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            result = await executor.submit(ScatterOperator(size=1), config)

        assert isinstance(result, JobClient)
        assert len(result.trials) == 1
        assert isinstance(result.trials[0], TrialClient)

    async def test_submit_empty_returns_empty_job_client(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            result = await executor.submit(ScatterOperator(size=0), config)

        assert isinstance(result, JobClient)
        assert result.trials == []

    async def test_submit_raises_on_nohup_start_error(self):
        mock_sandbox = _make_mock_sandbox()
        error_obs = MagicMock()
        error_obs.output = "some error"
        mock_sandbox.start_nohup_process = AsyncMock(return_value=(None, error_obs))

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            with pytest.raises(RuntimeError, match="Failed to start trial"):
                await executor.submit(ScatterOperator(size=1), config)


class TestJobExecutorWait:
    async def test_wait_returns_trial_result_list(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            job_client = await executor.submit(ScatterOperator(size=1), config)
            results = await executor.wait(job_client)

        assert isinstance(results, list)
        assert len(results) == 1

    async def test_wait_empty_job_client_returns_empty(self):
        executor = JobExecutor()
        results = await executor.wait(JobClient(trials=[]))
        assert results == []

    async def test_wait_process_failure_sets_exception_info(self):
        mock_sandbox = _make_mock_sandbox()
        # Process succeeds (exit_code=0) but wait reports failure (e.g. timeout).
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "timeout"))

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            results = await executor.run(ScatterOperator(size=1), config)

        assert len(results) == 1
        assert results[0].exception_info is not None


# ---------------------------------------------------------------------------
# sandbox close behavior
# ---------------------------------------------------------------------------


class TestJobExecutorSandboxClose:
    async def test_sandbox_not_closed_after_run(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            config = BashJobConfig(script="echo hi", job_name="test")
            executor = JobExecutor()
            await executor.run(ScatterOperator(size=1), config)

        assert mock_sandbox.close.call_count == 0


# ---------------------------------------------------------------------------
# _build_session_env
# ---------------------------------------------------------------------------


class TestBuildSessionEnv:
    def test_merges_oss_vars_with_config_env(self, monkeypatch):
        # Clear any leftover OSS vars to guarantee deterministic state.
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OSS_KEY", "value")

        from rock.sdk.envhub import EnvironmentConfig

        config = BashJobConfig(script="echo hi", environment=EnvironmentConfig(env={"X": "1"}))
        merged = JobExecutor._build_session_env(config)

        assert merged is not None
        assert merged["OSS_KEY"] == "value"
        assert merged["X"] == "1"

    def test_config_env_overrides_oss(self, monkeypatch):
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OSS_KEY", "process_val")

        from rock.sdk.envhub import EnvironmentConfig

        config = BashJobConfig(script="echo hi", environment=EnvironmentConfig(env={"OSS_KEY": "config_val"}))
        merged = JobExecutor._build_session_env(config)

        assert merged is not None
        assert merged["OSS_KEY"] == "config_val"

    def test_returns_none_when_empty(self, monkeypatch):
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)

        config = BashJobConfig(script="echo hi")  # default env={}
        merged = JobExecutor._build_session_env(config)

        assert merged is None


# ---------------------------------------------------------------------------
# G6: USER_DEFINED_LOGS paths
# ---------------------------------------------------------------------------


class TestExecutorPaths:
    """G6: scripts and nohup outputs must live under USER_DEFINED_LOGS, not /tmp."""

    async def test_do_submit_writes_script_under_user_defined_logs(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            await executor.submit(ScatterOperator(size=1), BashJobConfig(script="echo hi", job_name="p1"))

        # Inspect the path passed to write_file_by_path — must start with USER_DEFINED_LOGS
        write_call = mock_sandbox.write_file_by_path.call_args
        script_path = write_call.args[1] if len(write_call.args) >= 2 else write_call.kwargs["path"]
        assert script_path.startswith(
            USER_DEFINED_LOGS
        ), f"script path {script_path!r} must live under {USER_DEFINED_LOGS!r}, not /tmp"

    async def test_do_submit_passes_nohup_tmp_file_under_user_defined_logs(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            await executor.submit(ScatterOperator(size=1), BashJobConfig(script="echo hi", job_name="p2"))

        nohup_call = mock_sandbox.start_nohup_process.call_args
        tmp_file = nohup_call.kwargs["tmp_file"]
        assert tmp_file.startswith(USER_DEFINED_LOGS)


# ---------------------------------------------------------------------------
# G4: on_sandbox_ready hook called after start()
# ---------------------------------------------------------------------------


class TestExecutorOnSandboxReady:
    async def test_do_submit_calls_on_sandbox_ready_after_start(self):
        from rock.sdk.job.trial.bash import BashTrial

        mock_sandbox = _make_mock_sandbox()
        mock_sandbox._namespace = "ns"
        mock_sandbox._experiment_id = "exp"

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            trial = BashTrial(BashJobConfig(script="echo hi", job_name="t"))
            trial.on_sandbox_ready = AsyncMock()
            await executor._do_submit(trial)

        trial.on_sandbox_ready.assert_awaited_once_with(mock_sandbox)
        # Must be called AFTER sandbox.start()
        assert mock_sandbox.start.call_count == 1
