"""Compatibility exports for database-backed Job and Group metadata."""

from pydantic import BaseModel, Field

from rock.sdk.job.metadata.models import JobGroupMeta, JobMeta, RunMeta


class RunScoreSummary(BaseModel):
    """Deprecated non-persisted score summary; use JobGroupStatistics."""

    completed: int
    failed: int
    skipped: int
    avg_score: float
    total_score: float
    pass_rate: float
    scores: dict[str, float] = Field(default_factory=dict)


class RunJobRef(BaseModel):
    """Deprecated lightweight task reference."""

    task_id: str
    job_name: str


class RunJobStatus(BaseModel):
    """Deprecated lightweight Job status projection."""

    task_id: str
    job_name: str
    status: str = "unknown"
    sandbox_id: str | None = None
    score: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


__all__ = [
    "JobGroupMeta",
    "JobMeta",
    "RunJobRef",
    "RunJobStatus",
    "RunMeta",
    "RunScoreSummary",
]
