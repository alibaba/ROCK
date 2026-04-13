"""Result models for the Job system."""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from rock.sdk.agent.models.job.result import JobStatus
from rock.sdk.agent.models.trial.result import TrialResult


class TaskStatus(str, Enum):
    """Terminal status of a single task execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskResult(BaseModel):
    """Result produced by a single task execution."""

    task_id: str = ""
    status: TaskStatus = TaskStatus.COMPLETED
    output: str = ""
    exit_code: int = 0
    data: dict = Field(default_factory=dict)
    trial_results: list[TrialResult] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when the task completed successfully."""
        return self.status == TaskStatus.COMPLETED

    @property
    def score(self) -> float:
        """Average score across all trial results, 0.0 if none."""
        if not self.trial_results:
            return 0.0
        return sum(t.score for t in self.trial_results) / len(self.trial_results)


T = TypeVar("T", bound=BaseModel)


class JobResult(BaseModel, Generic[T]):
    """Aggregated result of a complete job run.

    Generic over task result type T:
      - JobResult[TaskResult]  — new Job system (task_results)
      - agent's JobResult uses TrialResult directly (trial_results field)
    """

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    labels: dict[str, str] = Field(default_factory=dict)
    task_results: list[T] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        """Average score across all task results, 0.0 if none."""
        if not self.task_results:
            return 0.0
        return sum(t.score for t in self.task_results) / len(self.task_results)

    @property
    def n_completed(self) -> int:
        """Number of completed items (items with .status == 'completed')."""
        return sum(1 for t in self.task_results if getattr(t, "status", None) == "completed")

    @property
    def n_failed(self) -> int:
        """Number of failed items (items with .status == 'failed')."""
        return sum(1 for t in self.task_results if getattr(t, "status", None) == "failed")
