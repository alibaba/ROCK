"""Public data models for database-backed Job and Group metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    PLANNED = "planned"
    STARTING = "starting"
    SANDBOX_READY = "sandbox_ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNRECOVERABLE = "unrecoverable"


class JobGroupStatus(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobGroupMode(str, Enum):
    SINGLE = "single"
    MULTI = "multi"
    FULL = "full"


class JobStatusCategory(str, Enum):
    ALL = "all"
    ACTIVE = "active"
    COMPLETED = "completed"
    UNSUCCESSFUL = "unsuccessful"
    NOT_COMPLETED = "not_completed"


ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.PLANNED,
        JobStatus.STARTING,
        JobStatus.SANDBOX_READY,
        JobStatus.RUNNING,
    }
)
UNSUCCESSFUL_JOB_STATUSES = frozenset(
    {
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.UNRECOVERABLE,
    }
)
NOT_COMPLETED_JOB_STATUSES = frozenset(set(JobStatus) - {JobStatus.COMPLETED})


class MetadataModel(BaseModel):
    """Shared validation and ORM conversion behavior."""

    model_config = ConfigDict(from_attributes=True)

    @field_validator(
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        check_fields=False,
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("metadata timestamps must include timezone information")
        return value


class JobMeta(MetadataModel):
    """Persisted metadata for one Job."""

    job_id: UUID = Field(default_factory=uuid4)
    namespace: str = Field(min_length=1, max_length=128)
    experiment_id: str = Field(min_length=1, max_length=128)
    job_name: str = Field(min_length=1, max_length=255)
    group_id: UUID | None = None
    task_id: str | None = Field(default=None, max_length=255)
    job_type: str = Field(min_length=1, max_length=32)
    status: JobStatus
    sandbox_id: str | None = Field(default=None, max_length=128)
    session: str | None = Field(default=None, max_length=128)
    pid: int | None = None
    exit_code: int | None = None
    score: float | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobGroupMeta(MetadataModel):
    """Minimal persisted metadata for a Group."""

    group_id: UUID = Field(default_factory=uuid4)
    namespace: str = Field(min_length=1, max_length=128)
    experiment_id: str = Field(min_length=1, max_length=128)
    mode: JobGroupMode
    status: JobGroupStatus
    dataset: str | None = Field(default=None, max_length=512)
    split: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None

    @property
    def run_id(self) -> UUID:
        """Compatibility view of the former Run identifier."""
        return self.group_id


RunMeta = JobGroupMeta


class JobUpdate(MetadataModel):
    """Fields that may change while a Job executes."""

    status: JobStatus | None = None
    sandbox_id: str | None = Field(default=None, max_length=128)
    session: str | None = Field(default=None, max_length=128)
    pid: int | None = None
    exit_code: int | None = None
    score: float | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def prevent_clearing_status(self) -> Self:
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("Job status cannot be cleared")
        return self


class JobUpdateItem(BaseModel):
    job_id: UUID
    changes: JobUpdate


class JobGroupUpdate(MetadataModel):
    """Mutable fields on a Group."""

    status: JobGroupStatus | None = None
    dataset: str | None = Field(default=None, max_length=512)
    split: str | None = Field(default=None, max_length=128)
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def prevent_clearing_status(self) -> Self:
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("Group status cannot be cleared")
        return self


class GroupJobQuery(BaseModel):
    category: JobStatusCategory | None = None
    statuses: set[JobStatus] | None = None
    task_ids: set[str] | None = None
    job_type: str | None = None

    @model_validator(mode="after")
    def validate_status_filter(self) -> Self:
        if self.category is not None and self.statuses is not None:
            raise ValueError("category and statuses cannot be used together")
        return self

    def resolved_statuses(self) -> frozenset[JobStatus] | None:
        """Resolve a convenience category into exact database statuses."""
        if self.statuses is not None:
            return frozenset(self.statuses)
        if self.category in {None, JobStatusCategory.ALL}:
            return None
        if self.category == JobStatusCategory.ACTIVE:
            return ACTIVE_JOB_STATUSES
        if self.category == JobStatusCategory.COMPLETED:
            return frozenset({JobStatus.COMPLETED})
        if self.category == JobStatusCategory.UNSUCCESSFUL:
            return UNSUCCESSFUL_JOB_STATUSES
        return NOT_COMPLETED_JOB_STATUSES


class GroupQuery(BaseModel):
    experiment_id: str | None = None
    statuses: set[JobGroupStatus] | None = None
    modes: set[JobGroupMode] | None = None
    dataset: str | None = None


class PageRequest(BaseModel):
    page_size: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None


class JobPage(BaseModel):
    items: list[JobMeta]
    total: int = Field(ge=0)
    next_cursor: str | None = None


class JobGroupPage(BaseModel):
    items: list[JobGroupMeta]
    total: int = Field(ge=0)
    next_cursor: str | None = None


class JobGroupStatistics(BaseModel):
    group_id: UUID
    total_jobs: int = Field(ge=0)
    active_jobs: int = Field(ge=0)
    completed_jobs: int = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    cancelled_jobs: int = Field(ge=0)
    unrecoverable_jobs: int = Field(ge=0)
    scored_jobs: int = Field(ge=0)
    avg_score: float
    total_score: float
    min_score: float | None = None
    max_score: float | None = None


class JobGroupDetail(BaseModel):
    group: JobGroupMeta
    statistics: JobGroupStatistics
    jobs: JobPage
