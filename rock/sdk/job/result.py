"""Result models for the Job system.

Base classes: TrialResult, JobStatus, JobResult[T].
Harbor-specific subclasses in rock.sdk.agent.models.trial.result.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# TrialResult base — 通用字段
# ---------------------------------------------------------------------------


class ExceptionInfo(BaseModel):
    """通用异常信息"""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class TrialResult(BaseModel):
    """单次执行结果的基类 — 通用字段

    Harbor 的 TrialResult 继承此类，添加 agent_info, verifier_result 等字段。
    子类可 override score 和 status properties。
    """

    task_name: str = ""
    exception_info: ExceptionInfo | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # G5: process-level outputs captured by JobExecutor
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        return 0.0

    @property
    def status(self) -> str:
        return "failed" if self.exception_info else "completed"

    @property
    def duration_sec(self) -> float:
        if self.started_at and self.finished_at:
            try:
                start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
                return (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        return 0.0


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
