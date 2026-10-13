"""SQLAlchemy schema for PostgreSQL-backed Job metadata."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    and_,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MetadataBase(DeclarativeBase):
    """Declarative base exported for migration tooling."""


class JobGroupRecord(MetadataBase):
    __tablename__ = "job_group_metadata"

    group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str | None] = mapped_column(String(512))
    split: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRecord(MetadataBase):
    __tablename__ = "job_metadata"

    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_group_metadata.group_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str | None] = mapped_column(String(255))
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String(128))
    session: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(BigInteger)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "ix_job_group_scope_created",
    JobGroupRecord.namespace,
    JobGroupRecord.experiment_id,
    JobGroupRecord.created_at.desc(),
)
Index(
    "ix_job_group_scope_status",
    JobGroupRecord.namespace,
    JobGroupRecord.experiment_id,
    JobGroupRecord.status,
)
Index(
    "ix_job_group_namespace_created",
    JobGroupRecord.namespace,
    JobGroupRecord.created_at.desc(),
    JobGroupRecord.group_id.desc(),
)
Index(
    "ix_job_scope_name_created",
    JobRecord.namespace,
    JobRecord.experiment_id,
    JobRecord.job_name,
    JobRecord.created_at.desc(),
)
_group_task_present = and_(JobRecord.group_id.is_not(None), JobRecord.task_id.is_not(None))
Index(
    "uq_job_group_task",
    JobRecord.group_id,
    JobRecord.task_id,
    unique=True,
    postgresql_where=_group_task_present,
    sqlite_where=_group_task_present,
)
Index("ix_job_group_status", JobRecord.group_id, JobRecord.status)
Index(
    "ix_job_sandbox",
    JobRecord.sandbox_id,
    postgresql_where=JobRecord.sandbox_id.is_not(None),
    sqlite_where=JobRecord.sandbox_id.is_not(None),
)
