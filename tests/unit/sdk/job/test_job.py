"""Tests for rock.sdk.job.job — Job Facade."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import rock.sdk.bench  # pre-import to avoid circular  # noqa: F401
import rock.sdk.job.trial.bash  # trigger BashTrial registration  # noqa: F401
from rock.sdk.job import Job
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import JobStatus


def _make_mock_sandbox():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-facade"
    # AsyncMock auto-creates child mocks for any attr access; force these
    # two back to None so AbstractTrial.on_sandbox_ready's default backfill
    # is a no-op (matching a real sandbox that reports no ns / exp_id).
    sandbox._namespace = None
    sandbox._experiment_id = None
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file_by_path = AsyncMock()
    sandbox.arun = AsyncMock()

    upload_obs = MagicMock()
    upload_obs.exit_code = 0
    sandbox.fs = AsyncMock()
    sandbox.fs.upload_dir = AsyncMock(return_value=upload_obs)

    sandbox.start_nohup_process = AsyncMock(return_value=(99, None))
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))

    obs = MagicMock()
    obs.output = "ok"
    obs.exit_code = 0
    sandbox.handle_nohup_output = AsyncMock(return_value=obs)
    return sandbox


# ---------------------------------------------------------------------------
# run() — full lifecycle
# ---------------------------------------------------------------------------


class TestJobRun:
    async def test_run_returns_completed_result_on_success(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="test")).run()

        assert result.status == JobStatus.COMPLETED
        assert len(result.trial_results) == 1

    async def test_run_returns_failed_status_when_trial_fails(self):
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "timeout"))

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="test")).run()

        assert result.status == JobStatus.FAILED
        assert len(result.trial_results) == 1
        assert result.trial_results[0].exception_info is not None


# ---------------------------------------------------------------------------
# submit() / wait() separately
# ---------------------------------------------------------------------------


class TestJobSubmitWait:
    async def test_submit_then_wait_equivalent_to_run(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="echo hi", job_name="test"))
            await job.submit()
            result = await job.wait()

        assert result.status == JobStatus.COMPLETED
        assert len(result.trial_results) == 1

    async def test_wait_without_submit_raises_runtime_error(self):
        job = Job(BashJobConfig(script="echo hi", job_name="test"))
        with pytest.raises(RuntimeError, match="No submitted job"):
            await job.wait()


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestJobCancel:
    async def test_cancel_kills_all_trial_sandboxes(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="echo hi", job_name="test"))
            await job.submit()
            await job.cancel()

        assert mock_sandbox.arun.called
        # Extract the cmd kwarg from the last arun call
        call = mock_sandbox.arun.call_args
        cmd = call.kwargs.get("cmd", "")
        assert "kill" in cmd

    async def test_cancel_without_submit_is_noop(self):
        job = Job(BashJobConfig(script="echo hi", job_name="test"))
        # Should not raise
        await job.cancel()


# ---------------------------------------------------------------------------
# Operator parameter
# ---------------------------------------------------------------------------


class TestJobOperator:
    async def test_custom_operator_with_size_two_produces_two_trials(self):
        mocks = [_make_mock_sandbox() for _ in range(2)]
        with patch("rock.sdk.job.executor.Sandbox", side_effect=mocks):
            result = await Job(
                BashJobConfig(script="echo hi", job_name="test"),
                operator=ScatterOperator(size=2),
            ).run()

        assert len(result.trial_results) == 2

    async def test_default_operator_is_scatter_size_one(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="test")).run()

        assert len(result.trial_results) == 1


# ---------------------------------------------------------------------------
# _build_result
# ---------------------------------------------------------------------------


class TestJobBuildResult:
    async def test_build_result_uses_config_labels(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="test", labels={"team": "rl"})).run()

        assert result.labels == {"team": "rl"}

    async def test_build_result_sets_job_id_from_job_name(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="my-job")).run()

        assert result.job_id == "my-job"

    async def test_build_result_any_failure_marks_job_failed(self):
        # Single trial, but force failure -> overall FAILED
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "err"))

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(
                BashJobConfig(script="echo hi", job_name="test"),
                operator=ScatterOperator(size=1),
            ).run()

        assert result.status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# Multi-sub-trial flattening (G1 regression)
# ---------------------------------------------------------------------------


class TestJobFlattenMultiSubTrials:
    """G1: when HarborTrial.collect returns list[N], JobResult.trial_results must have N entries."""

    async def test_run_flattens_list_returning_collect_into_job_result(self):
        from rock.sdk.job.config import JobConfig
        from rock.sdk.job.result import TrialResult
        from rock.sdk.job.trial.abstract import AbstractTrial
        from rock.sdk.job.trial.registry import register_trial

        class MultiCfg(JobConfig):
            pass

        class MultiTrial(AbstractTrial):
            async def setup(self, sandbox):
                pass

            def build(self) -> str:
                return "echo hi"

            async def collect(self, sandbox, output, exit_code):
                return [TrialResult(task_name=f"sub-{i}") for i in range(3)]

        register_trial(MultiCfg, MultiTrial)

        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(MultiCfg(job_name="multi")).run()

        assert len(result.trial_results) == 3, f"expected 3 flattened sub-trials, got {len(result.trial_results)}"
        assert {t.task_name for t in result.trial_results} == {"sub-0", "sub-1", "sub-2"}
        assert result.status == JobStatus.COMPLETED

    async def test_run_still_accepts_single_trial_result(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="single")).run()

        assert len(result.trial_results) == 1
        assert result.status == JobStatus.COMPLETED

    async def test_timeout_tags_every_sub_trial_in_list(self):
        """G1: on timeout, every sub-trial in a list result gets a synthetic ProcessTimeout —
        except those that already carry their own exception_info (preserved as-is)."""
        from rock.sdk.job.config import JobConfig
        from rock.sdk.job.result import ExceptionInfo, TrialResult
        from rock.sdk.job.trial.abstract import AbstractTrial
        from rock.sdk.job.trial.registry import register_trial

        class PreExistingCfg(JobConfig):
            pass

        class PreExistingTrial(AbstractTrial):
            async def setup(self, sandbox):
                pass

            def build(self) -> str:
                return "echo hi"

            async def collect(self, sandbox, output, exit_code):
                return [
                    TrialResult(task_name="sub-0"),
                    TrialResult(
                        task_name="sub-1",
                        exception_info=ExceptionInfo(
                            exception_type="OwnError",
                            exception_message="from trial",
                        ),
                    ),
                    TrialResult(task_name="sub-2"),
                ]

        register_trial(PreExistingCfg, PreExistingTrial)

        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "timed out"))

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(PreExistingCfg(job_name="multi-timeout")).run()

        assert result.status == JobStatus.FAILED
        assert len(result.trial_results) == 3
        by_name = {t.task_name: t for t in result.trial_results}
        assert by_name["sub-0"].exception_info.exception_type == "ProcessTimeout"
        assert by_name["sub-2"].exception_info.exception_type == "ProcessTimeout"
        # Pre-existing exception_info on sub-1 must NOT be overwritten
        assert by_name["sub-1"].exception_info.exception_type == "OwnError"
        assert by_name["sub-1"].exception_info.exception_message == "from trial"


# ---------------------------------------------------------------------------
# G5: raw_output / exit_code surfaced on JobResult
# ---------------------------------------------------------------------------


class TestJobResultRawOutputAndExitCode:
    """G5: JobResult must surface raw_output and exit_code from the sandbox process."""

    async def test_run_populates_raw_output_from_obs(self):
        mock_sandbox = _make_mock_sandbox()
        obs = MagicMock()
        obs.output = "hello from sandbox"
        obs.exit_code = 0
        mock_sandbox.handle_nohup_output = AsyncMock(return_value=obs)

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="echo hi", job_name="test")).run()

        assert result.raw_output == "hello from sandbox"
        assert result.exit_code == 0

    async def test_run_propagates_nonzero_exit_code(self):
        mock_sandbox = _make_mock_sandbox()
        obs = MagicMock()
        obs.output = "err"
        obs.exit_code = 7
        mock_sandbox.handle_nohup_output = AsyncMock(return_value=obs)

        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            result = await Job(BashJobConfig(script="false", job_name="test")).run()

        assert result.exit_code == 7
        assert result.raw_output == "err"
