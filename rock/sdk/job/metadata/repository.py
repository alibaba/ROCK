"""Transactional repository for Job and Group metadata."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from rock.sdk.job.metadata.errors import (
    MetadataConflictError,
    MetadataConstraintError,
    MetadataNotFoundError,
    MetadataPaginationError,
    MetadataValidationError,
)
from rock.sdk.job.metadata.models import (
    ACTIVE_JOB_STATUSES,
    GroupJobQuery,
    GroupQuery,
    JobGroupDetail,
    JobGroupMeta,
    JobGroupPage,
    JobGroupStatistics,
    JobGroupUpdate,
    JobMeta,
    JobPage,
    JobStatus,
    JobUpdate,
    JobUpdateItem,
    PageRequest,
    utc_now,
)
from rock.sdk.job.metadata.schema import JobGroupRecord, JobRecord

_GROUP_FIELDS = tuple(JobGroupMeta.model_fields)
_JOB_FIELDS = tuple(JobMeta.model_fields)


def _as_database_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _model_values(model: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    values = model.model_dump(exclude_unset=exclude_unset)
    return {key: _as_database_value(value) for key, value in values.items()}


def _aware(value: datetime | None) -> datetime | None:
    # SQLite does not preserve timezone metadata. PostgreSQL returns aware values.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _group_model(record: JobGroupRecord) -> JobGroupMeta:
    values = {field: getattr(record, field) for field in _GROUP_FIELDS}
    for field in ("created_at", "updated_at", "finished_at"):
        values[field] = _aware(values[field])
    return JobGroupMeta.model_validate(values)


def _job_model(record: JobRecord) -> JobMeta:
    values = {field: getattr(record, field) for field in _JOB_FIELDS}
    for field in ("created_at", "updated_at", "started_at", "finished_at"):
        values[field] = _aware(values[field])
    return JobMeta.model_validate(values)


def _translate_integrity_error(exc: IntegrityError) -> Exception:
    original = exc.orig
    code = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
    message = str(original).lower()
    if code == "23505" or "unique constraint" in message:
        return MetadataConflictError("metadata already exists or violates a unique constraint")
    return MetadataConstraintError("metadata violates a database constraint")


class JobMetadataRepository:
    """Synchronous metadata access using a caller-owned SQLAlchemy session factory."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def create_group(self, group: JobGroupMeta) -> JobGroupMeta:
        try:
            with self._session_factory.begin() as session:
                record = JobGroupRecord(**_model_values(group))
                session.add(record)
                session.flush()
                return _group_model(record)
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def create_job(self, job: JobMeta) -> JobMeta:
        try:
            with self._session_factory.begin() as session:
                self._validate_job_group(session, job)
                record = JobRecord(**_model_values(job))
                session.add(record)
                session.flush()
                return _job_model(record)
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def create_group_with_jobs(
        self,
        group: JobGroupMeta,
        jobs: Sequence[JobMeta],
    ) -> JobGroupMeta:
        try:
            with self._session_factory.begin() as session:
                group_record = JobGroupRecord(**_model_values(group))
                session.add(group_record)
                session.flush()
                for job in jobs:
                    self._validate_job_scope(job, group)
                    session.add(JobRecord(**_model_values(job)))
                session.flush()
                return _group_model(group_record)
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def get_group(self, group_id: UUID) -> JobGroupMeta | None:
        with self._session_factory() as session:
            record = session.get(JobGroupRecord, group_id)
            return _group_model(record) if record is not None else None

    def get_job(self, job_id: UUID) -> JobMeta | None:
        """Return the complete metadata record identified by its unique Job ID."""
        with self._session_factory() as session:
            record = session.get(JobRecord, job_id)
            return _job_model(record) if record is not None else None

    def update_group(
        self,
        group_id: UUID,
        changes: JobGroupUpdate,
    ) -> JobGroupMeta:
        try:
            with self._session_factory.begin() as session:
                record = session.get(JobGroupRecord, group_id)
                if record is None:
                    raise MetadataNotFoundError(f"Group {group_id} does not exist")
                self._apply_changes(record, changes)
                session.flush()
                return _group_model(record)
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def update_job(self, job_id: UUID, changes: JobUpdate) -> JobMeta:
        try:
            with self._session_factory.begin() as session:
                record = session.get(JobRecord, job_id)
                if record is None:
                    raise MetadataNotFoundError(f"Job {job_id} does not exist")
                self._apply_changes(record, changes)
                session.flush()
                return _job_model(record)
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def batch_update_jobs(
        self,
        updates: Sequence[JobUpdateItem],
    ) -> list[JobMeta]:
        try:
            with self._session_factory.begin() as session:
                records: list[JobRecord] = []
                for item in updates:
                    record = session.get(JobRecord, item.job_id)
                    if record is None:
                        raise MetadataNotFoundError(f"Job {item.job_id} does not exist")
                    self._apply_changes(record, item.changes)
                    records.append(record)
                session.flush()
                return [_job_model(record) for record in records]
        except IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def list_group_jobs(
        self,
        group_id: UUID,
        query: GroupJobQuery | None = None,
        pagination: PageRequest | None = None,
    ) -> JobPage:
        query = query or GroupJobQuery()
        with self._session_factory() as session:
            self._require_group(session, group_id)
            statement = select(JobRecord).where(JobRecord.group_id == group_id)
            statement = self._filter_group_jobs(statement, query)
            total = self._count_statement(session, statement)

            statement = statement.order_by(JobRecord.created_at.asc(), JobRecord.job_id.asc())
            if pagination is not None:
                if pagination.cursor is not None:
                    created_at, record_id = self._decode_cursor(pagination.cursor)
                    statement = statement.where(
                        or_(
                            JobRecord.created_at > created_at,
                            and_(
                                JobRecord.created_at == created_at,
                                JobRecord.job_id > record_id,
                            ),
                        )
                    )
                statement = statement.limit(pagination.page_size + 1)

            records = list(session.scalars(statement))
            records, next_cursor = self._finish_page(records, pagination)
            return JobPage(
                items=[_job_model(record) for record in records],
                total=total,
                next_cursor=next_cursor,
            )

    def list_namespace_groups(
        self,
        namespace: str,
        query: GroupQuery | None = None,
        pagination: PageRequest | None = None,
    ) -> JobGroupPage:
        if not namespace:
            raise MetadataValidationError("namespace must not be empty")
        query = query or GroupQuery()
        with self._session_factory() as session:
            statement = select(JobGroupRecord).where(JobGroupRecord.namespace == namespace)
            if query.experiment_id is not None:
                statement = statement.where(JobGroupRecord.experiment_id == query.experiment_id)
            if query.statuses is not None:
                statement = statement.where(
                    JobGroupRecord.status.in_([_as_database_value(status) for status in query.statuses])
                )
            if query.modes is not None:
                statement = statement.where(JobGroupRecord.mode.in_([_as_database_value(mode) for mode in query.modes]))
            if query.dataset is not None:
                statement = statement.where(JobGroupRecord.dataset == query.dataset)

            total = self._count_statement(session, statement)
            statement = statement.order_by(
                JobGroupRecord.created_at.desc(),
                JobGroupRecord.group_id.desc(),
            )
            if pagination is not None:
                if pagination.cursor is not None:
                    created_at, record_id = self._decode_cursor(pagination.cursor)
                    statement = statement.where(
                        or_(
                            JobGroupRecord.created_at < created_at,
                            and_(
                                JobGroupRecord.created_at == created_at,
                                JobGroupRecord.group_id < record_id,
                            ),
                        )
                    )
                statement = statement.limit(pagination.page_size + 1)

            records = list(session.scalars(statement))
            records, next_cursor = self._finish_page(records, pagination)
            return JobGroupPage(
                items=[_group_model(record) for record in records],
                total=total,
                next_cursor=next_cursor,
            )

    def get_group_statistics(self, group_id: UUID) -> JobGroupStatistics:
        with self._session_factory() as session:
            self._require_group(session, group_id)
            status_rows = session.execute(
                select(JobRecord.status, func.count()).where(JobRecord.group_id == group_id).group_by(JobRecord.status)
            ).all()
            status_counts = {status: count for status, count in status_rows}
            score_row = session.execute(
                select(
                    func.count(JobRecord.score),
                    func.avg(JobRecord.score),
                    func.sum(JobRecord.score),
                    func.min(JobRecord.score),
                    func.max(JobRecord.score),
                ).where(JobRecord.group_id == group_id)
            ).one()

        active_values = {status.value for status in ACTIVE_JOB_STATUSES}
        return JobGroupStatistics(
            group_id=group_id,
            total_jobs=sum(status_counts.values()),
            active_jobs=sum(count for status, count in status_counts.items() if status in active_values),
            completed_jobs=status_counts.get(JobStatus.COMPLETED.value, 0),
            failed_jobs=status_counts.get(JobStatus.FAILED.value, 0),
            cancelled_jobs=status_counts.get(JobStatus.CANCELLED.value, 0),
            unrecoverable_jobs=status_counts.get(JobStatus.UNRECOVERABLE.value, 0),
            scored_jobs=score_row[0],
            avg_score=float(score_row[1] or 0),
            total_score=float(score_row[2] or 0),
            min_score=score_row[3],
            max_score=score_row[4],
        )

    def get_group_detail(
        self,
        group_id: UUID,
        query: GroupJobQuery | None = None,
        pagination: PageRequest | None = None,
    ) -> JobGroupDetail:
        group = self.get_group(group_id)
        if group is None:
            raise MetadataNotFoundError(f"Group {group_id} does not exist")
        return JobGroupDetail(
            group=group,
            statistics=self.get_group_statistics(group_id),
            jobs=self.list_group_jobs(group_id, query, pagination),
        )

    @staticmethod
    def _apply_changes(record: JobGroupRecord | JobRecord, changes: Any) -> None:
        for field, value in _model_values(changes, exclude_unset=True).items():
            setattr(record, field, value)
        record.updated_at = utc_now()

    @staticmethod
    def _validate_job_scope(job: JobMeta, group: JobGroupMeta) -> None:
        if job.group_id != group.group_id:
            raise MetadataConstraintError("Job group_id must match the Group being created")
        if job.namespace != group.namespace or job.experiment_id != group.experiment_id:
            raise MetadataConstraintError("Job namespace and experiment_id must match its Group")

    @staticmethod
    def _require_group(session: Session, group_id: UUID) -> JobGroupRecord:
        group = session.get(JobGroupRecord, group_id)
        if group is None:
            raise MetadataNotFoundError(f"Group {group_id} does not exist")
        return group

    @staticmethod
    def _filter_group_jobs(
        statement: Select[tuple[JobRecord]],
        query: GroupJobQuery,
    ) -> Select[tuple[JobRecord]]:
        statuses = query.resolved_statuses()
        if statuses is not None:
            statement = statement.where(JobRecord.status.in_([status.value for status in statuses]))
        if query.task_ids is not None:
            statement = statement.where(JobRecord.task_id.in_(query.task_ids))
        if query.job_type is not None:
            statement = statement.where(JobRecord.job_type == query.job_type)
        return statement

    @staticmethod
    def _count_statement(session: Session, statement: Select[Any]) -> int:
        count_statement = select(func.count()).select_from(statement.order_by(None).subquery())
        return int(session.scalar(count_statement) or 0)

    @classmethod
    def _finish_page(
        cls,
        records: list[JobRecord] | list[JobGroupRecord],
        pagination: PageRequest | None,
    ) -> tuple[list[Any], str | None]:
        if pagination is None or len(records) <= pagination.page_size:
            return records, None
        page_records = records[: pagination.page_size]
        last = page_records[-1]
        record_id = getattr(last, "job_id", None) or last.group_id
        return page_records, cls._encode_cursor(last.created_at, record_id)

    @staticmethod
    def _encode_cursor(created_at: datetime, record_id: UUID) -> str:
        payload = json.dumps(
            {
                "created_at": _aware(created_at).isoformat(),
                "id": str(record_id),
            },
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
            created_at = datetime.fromisoformat(payload["created_at"])
            record_id = UUID(payload["id"])
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError("cursor datetime has no timezone")
            return created_at, record_id
        except (
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise MetadataPaginationError("invalid metadata pagination cursor") from exc

    @staticmethod
    def _validate_job_group(session: Session, job: JobMeta) -> None:
        if job.group_id is None:
            return
        group_record = session.get(JobGroupRecord, job.group_id)
        if group_record is None:
            raise MetadataConstraintError(f"Group {job.group_id} does not exist")
        if job.namespace != group_record.namespace or job.experiment_id != group_record.experiment_id:
            raise MetadataConstraintError("Job namespace and experiment_id must match its Group")
