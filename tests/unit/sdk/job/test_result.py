"""Tests for rock.sdk.job.result — TaskResult, JobResult models."""

from __future__ import annotations

import pytest

from rock.sdk.agent.models.trial.result import TrialResult, VerifierResult
from rock.sdk.job.result import JobResult, JobStatus, TaskResult, TaskStatus

# ---------------------------------------------------------------------------
# TaskStatus enum
# ---------------------------------------------------------------------------


class TestTaskStatus:
    def test_values(self):
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"

    def test_is_str(self):
        assert isinstance(TaskStatus.COMPLETED, str)


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------


class TestTaskResult:
    def test_defaults(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED)
        assert tr.task_id == "t1"
        assert tr.status == TaskStatus.COMPLETED
        assert tr.output == ""
        assert tr.exit_code == 0
        assert tr.data == {}
        assert tr.trial_results == []

    def test_success_property_completed(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED)
        assert tr.success is True

    def test_success_property_failed(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.FAILED)
        assert tr.success is False

    def test_success_property_cancelled(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.CANCELLED)
        assert tr.success is False

    def test_score_empty_trials(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED)
        assert tr.score == 0.0

    def test_score_single_trial(self):
        trial = TrialResult(verifier_result=VerifierResult(rewards={"reward": 0.8}))
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED, trial_results=[trial])
        assert tr.score == pytest.approx(0.8)

    def test_score_multiple_trials(self):
        trials = [
            TrialResult(verifier_result=VerifierResult(rewards={"reward": 0.6})),
            TrialResult(verifier_result=VerifierResult(rewards={"reward": 1.0})),
        ]
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED, trial_results=trials)
        assert tr.score == pytest.approx(0.8)

    def test_score_with_zero_score_trials(self):
        trials = [
            TrialResult(),  # no verifier_result -> score 0.0
            TrialResult(verifier_result=VerifierResult(rewards={"reward": 0.4})),
        ]
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED, trial_results=trials)
        assert tr.score == pytest.approx(0.2)

    def test_output_and_exit_code(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.FAILED, output="error msg", exit_code=1)
        assert tr.output == "error msg"
        assert tr.exit_code == 1

    def test_data_field(self):
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED, data={"key": "value"})
        assert tr.data == {"key": "value"}


# ---------------------------------------------------------------------------
# JobStatus enum
# ---------------------------------------------------------------------------


class TestJobStatus:
    def test_values(self):
        # JobStatus reused from rock.sdk.agent.models.job.result
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.CANCELLED == "cancelled"

    def test_is_str(self):
        assert isinstance(JobStatus.COMPLETED, str)


# ---------------------------------------------------------------------------
# JobResult
# ---------------------------------------------------------------------------


class TestJobResult:
    def _make_task_result(self, task_id: str, status: TaskStatus, score: float = 0.0) -> TaskResult:
        trials = []
        if score > 0:
            trials = [TrialResult(verifier_result=VerifierResult(rewards={"reward": score}))]
        return TaskResult(task_id=task_id, status=status, trial_results=trials)

    def test_defaults(self):
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED)
        assert jr.job_id == "j1"
        assert jr.status == JobStatus.COMPLETED
        assert jr.labels == {}
        assert jr.task_results == []

    def test_score_empty_tasks(self):
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED)
        assert jr.score == 0.0

    def test_score_single_task(self):
        tr = self._make_task_result("t1", TaskStatus.COMPLETED, score=0.9)
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=[tr])
        assert jr.score == pytest.approx(0.9)

    def test_score_multiple_tasks(self):
        tasks = [
            self._make_task_result("t1", TaskStatus.COMPLETED, score=0.6),
            self._make_task_result("t2", TaskStatus.COMPLETED, score=1.0),
        ]
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=tasks)
        assert jr.score == pytest.approx(0.8)

    def test_n_completed(self):
        tasks = [
            self._make_task_result("t1", TaskStatus.COMPLETED),
            self._make_task_result("t2", TaskStatus.FAILED),
            self._make_task_result("t3", TaskStatus.COMPLETED),
        ]
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=tasks)
        assert jr.n_completed == 2

    def test_n_failed(self):
        tasks = [
            self._make_task_result("t1", TaskStatus.COMPLETED),
            self._make_task_result("t2", TaskStatus.FAILED),
            self._make_task_result("t3", TaskStatus.FAILED),
        ]
        jr = JobResult(job_id="j1", status=JobStatus.FAILED, task_results=tasks)
        assert jr.n_failed == 2

    def test_n_completed_and_n_failed_empty(self):
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED)
        assert jr.n_completed == 0
        assert jr.n_failed == 0

    def test_trial_results_property_with_single_task(self):
        """trial_results returns task_results[0].trial_results for Harbor backward compat."""
        trial = TrialResult(task_name="bench", trial_name="trial-0")
        tr = TaskResult(task_id="t1", status=TaskStatus.COMPLETED, trial_results=[trial])
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=[tr])
        assert jr.trial_results == [trial]

    def test_trial_results_property_empty(self):
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED)
        assert jr.trial_results == []

    def test_trial_results_property_first_task_only(self):
        """Even with multiple tasks, only returns first task's trial_results."""
        trial_a = TrialResult(task_name="a")
        trial_b = TrialResult(task_name="b")
        tasks = [
            TaskResult(task_id="t1", status=TaskStatus.COMPLETED, trial_results=[trial_a]),
            TaskResult(task_id="t2", status=TaskStatus.COMPLETED, trial_results=[trial_b]),
        ]
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=tasks)
        assert jr.trial_results == [trial_a]

    def test_labels(self):
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, labels={"env": "test", "team": "ml"})
        assert jr.labels == {"env": "test", "team": "ml"}

    def test_score_with_cancelled_tasks(self):
        """Cancelled tasks have score 0.0 and are included in average."""
        tasks = [
            self._make_task_result("t1", TaskStatus.COMPLETED, score=1.0),
            TaskResult(task_id="t2", status=TaskStatus.CANCELLED),
        ]
        jr = JobResult(job_id="j1", status=JobStatus.COMPLETED, task_results=tasks)
        assert jr.score == pytest.approx(0.5)
