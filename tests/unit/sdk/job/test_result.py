"""Tests for rock.sdk.job.result — JobStatus, JobResult[T]."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from rock.sdk.job.result import JobResult, JobStatus, RewardTrialResult


class _Item(BaseModel):
    """Stub item for testing JobResult[T]."""

    name: str = ""
    status: str = "completed"

    @property
    def score(self) -> float:
        return 1.0 if self.status == "completed" else 0.0


class TestJobStatus:
    def test_values(self):
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.CANCELLED == "cancelled"

    def test_is_str(self):
        assert isinstance(JobStatus.COMPLETED, str)


class TestJobResult:
    def test_defaults(self):
        r = JobResult()
        assert r.job_id == ""
        assert r.status == JobStatus.COMPLETED
        assert r.labels == {}
        assert r.trial_results == []
        assert r.raw_output == ""
        assert r.exit_code == 0

    def test_score_empty(self):
        assert JobResult().score == 0.0

    def test_score_with_items(self):
        r = JobResult[_Item](trial_results=[_Item(), _Item(status="failed")])
        assert r.score == pytest.approx(0.5)

    def test_n_completed(self):
        r = JobResult[_Item](
            trial_results=[_Item(), _Item(status="failed"), _Item()],
        )
        assert r.n_completed == 2

    def test_n_failed(self):
        r = JobResult[_Item](
            trial_results=[_Item(status="failed"), _Item(status="failed")],
        )
        assert r.n_failed == 2

    def test_labels(self):
        r = JobResult(labels={"env": "test"})
        assert r.labels == {"env": "test"}

    def test_raw_output_and_exit_code(self):
        r = JobResult(raw_output="output", exit_code=1)
        assert r.raw_output == "output"
        assert r.exit_code == 1


class TestRewardTrialResult:
    def test_score_comes_from_verifier_reward(self):
        result = RewardTrialResult.from_reward_json(
            {
                "task_name": "task-1",
                "trial_name": "task-1__abc",
                "verifier_result": {"rewards": {"reward": 0.85, "task_score": 0.85}},
                "exception_info": None,
            }
        )

        assert result.task_name == "task-1"
        assert result.trial_name == "task-1__abc"
        assert result.score == pytest.approx(0.85)
