"""Result models for the Job system.

Base classes: TrialResult, JobStatus, JobResult[T].
Reward-protocol result models live in rock.sdk.reward.result.
"""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from rock.sdk.result import ExceptionInfo, TrialResult
from rock.sdk.reward.result import AgentInfo, AgentResult, ModelInfo, RewardTrialResult, TimingInfo, VerifierResult

__all__ = [
    "AgentInfo",
    "AgentResult",
    "ExceptionInfo",
    "JobResult",
    "JobStatus",
    "ModelInfo",
    "RewardTrialResult",
    "TimingInfo",
    "TrialResult",
    "VerifierResult",
]

# ---------------------------------------------------------------------------
# JobStatus + JobResult[T]
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Job status enum."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


T = TypeVar("T", bound=TrialResult)


class JobResult(BaseModel, Generic[T]):
    """Aggregated result of a complete job run.

    Generic over trial result type T:
      - JobResult[TrialResult]       — base (new Job system)
      - JobResult[HarborTrialResult] — Harbor agent system
    """

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    labels: dict[str, str] = Field(default_factory=dict)
    trial_results: list[T] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trial_results:
            return 0.0
        return sum(t.score for t in self.trial_results) / len(self.trial_results)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "completed")

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "failed")
