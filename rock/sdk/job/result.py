"""Result models for the Job system.

Base classes: TrialResult, JobStatus, JobResult[T].
Harbor-specific subclasses in rock.sdk.agent.models.trial.result.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# TrialResult base — common fields
# ---------------------------------------------------------------------------


class ExceptionInfo(BaseModel):
    """General exception info."""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class TrialResult(BaseModel):
    """Base class for a single execution result — common fields.

    Harbor's TrialResult inherits this class and adds agent_info, verifier_result, etc.
    Subclasses can override the score and status properties.
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


# ---------------------------------------------------------------------------
# BashTrialResult — parses score from stdout Score Summary
# ---------------------------------------------------------------------------

_SCORE_RE = re.compile(
    r"===\s*Score\s+Summary\s*===\s*\n.*?(?:score|task_score):\s*([\d.]+)",
    re.DOTALL | re.IGNORECASE,
)


class BashTrialResult(TrialResult):
    """TrialResult for Bash Jobs — parses score from stdout Score Summary block.

    BenchHub Bash Jobs emit a ``=== Score Summary ===`` block at the end of
    stdout. This subclass extracts the score so it surfaces in JobResult
    and can be reported to tracking backends.
    """

    _parsed_score: float | None = None

    @property
    def score(self) -> float:
        if self._parsed_score is not None:
            return self._parsed_score
        if not self.raw_output:
            return 0.0
        match = _SCORE_RE.search(self.raw_output)
        if match:
            try:
                self._parsed_score = float(match.group(1))
                return self._parsed_score
            except (ValueError, TypeError):
                pass
        return 0.0
