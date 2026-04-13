"""Result models for the Job system."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from rock.sdk.agent.models.trial.result import TrialResult, VerifierResult  # noqa: F401


class TaskStatus(str, Enum):
    """Terminal status of a single task execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskResult(BaseModel):
    """Result produced by a single task execution."""

    task_id: str
    status: TaskStatus
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


class JobStatus(str, Enum):
    """Terminal status of a job."""

    COMPLETED = "completed"
    FAILED = "failed"


class JobResult(BaseModel):
    """Aggregated result of a complete job run."""

    job_id: str
    status: JobStatus
    labels: dict[str, str] = Field(default_factory=dict)
    task_results: list[TaskResult] = Field(default_factory=list)

    @property
    def score(self) -> float:
        """Average score across all task results, 0.0 if none."""
        if not self.task_results:
            return 0.0
        return sum(t.score for t in self.task_results) / len(self.task_results)

    @property
    def n_completed(self) -> int:
        """Number of tasks with COMPLETED status."""
        return sum(1 for t in self.task_results if t.status == TaskStatus.COMPLETED)

    @property
    def n_failed(self) -> int:
        """Number of tasks with FAILED status."""
        return sum(1 for t in self.task_results if t.status == TaskStatus.FAILED)

    @property
    def trial_results(self) -> list[TrialResult]:
        """Return first task's trial_results for Harbor backward compatibility."""
        if self.task_results:
            return self.task_results[0].trial_results
        return []
