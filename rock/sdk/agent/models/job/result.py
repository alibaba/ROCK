"""Job result models aligned with harbor.models.job.result."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from rock.sdk.agent.models.trial.result import TrialResult


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobResult(BaseModel):
    """Aligned with harbor.models.job.result.JobResult"""

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    trial_results: list[TrialResult] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trial_results:
            return 0.0
        scores = [t.score for t in self.trial_results]
        return sum(scores) / len(scores)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "completed")

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "failed")
