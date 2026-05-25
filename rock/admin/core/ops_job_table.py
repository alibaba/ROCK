"""OpsJobTable: admin ops job CRUD over DatabaseProvider.

Persists ops-job state shared across all admin pods, so POST (submit job) and
GET (query status) can land on different pods without losing job_id.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import _retry_on_disconnect
from rock.admin.core.schema import OpsJobRecord
from rock.logger import init_logger

logger = init_logger(__name__)


# Job lifecycle states (also used as result.status in API responses).
JOB_STATUS_ACCEPTED = "accepted"  # written to DB, not yet running
JOB_STATUS_RUNNING = "running"  # background task picked it up
JOB_STATUS_COMPLETED = "completed"  # all tasks finished, results recorded
JOB_STATUS_FAILED = "failed"  # background task raised
JOB_STATUS_RATE_LIMITED = "rate_limited"  # rejected by cooldown gate (no DB row)
JOB_STATUS_REJECTED = "rejected"  # rejected by whitelist (no DB row)
JOB_STATUS_NOT_FOUND = "not_found"  # GET returned no row


class OpsJobTable:
    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    @_retry_on_disconnect
    async def insert(self, job: dict) -> None:
        """Insert a new ops job record.

        Raises ``IntegrityError`` on duplicate job_id (shouldn't happen — uuid4).
        """
        record = OpsJobRecord(**job)
        async with AsyncSession(self._db.engine) as session:
            session.add(record)
            await session.commit()

    @_retry_on_disconnect
    async def get(self, job_id: str) -> dict | None:
        """Return a job row as plain dict, or None if not found."""
        async with AsyncSession(self._db.engine) as session:
            record = await session.get(OpsJobRecord, job_id)
            return None if record is None else _row_to_dict(record)

    @_retry_on_disconnect
    async def update_status(
        self,
        job_id: str,
        status: str,
        results: Any = None,
        error: str | None = None,
    ) -> bool:
        """Update job status (and optionally results/error). Returns False if not found."""
        async with AsyncSession(self._db.engine) as session:
            record = await session.get(OpsJobRecord, job_id)
            if record is None:
                logger.warning(f"update_status: job {job_id} not found")
                return False
            record.status = status
            if results is not None:
                record.results = results
            if error is not None:
                record.error = error[:2048]
            if status in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED):
                record.completed_at = time.time()
            await session.commit()
            return True

    @_retry_on_disconnect
    async def list_recent_by_tasks(self, task_types: list[str], since_epoch: float) -> list[dict]:
        """Return recent jobs (submitted_at >= since_epoch) that touch any of task_types.

        Used by the rate-limit gate so cooldown is shared across admin pods, not
        per-process. Returns at most the most recent 100 rows for safety.
        """
        if not task_types:
            return []
        async with AsyncSession(self._db.engine) as session:
            stmt = (
                select(OpsJobRecord)
                .where(OpsJobRecord.submitted_at >= since_epoch)
                .order_by(desc(OpsJobRecord.submitted_at))
                .limit(100)
            )
            rows = (await session.execute(stmt)).scalars().all()
            # JSONB field, list-of-strings filter done in Python (small N)
            return [_row_to_dict(r) for r in rows if set(r.tasks or []) & set(task_types)]


def _row_to_dict(row: OpsJobRecord) -> dict:
    return {c.name: getattr(row, c.name) for c in OpsJobRecord.__table__.columns}
