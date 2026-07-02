"""SchedulerTaskTable: single-table CRUD for scheduler task executions.

Tasks are grouped by taskset_id (one group per API call).
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import select

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import _retry_on_disconnect
from rock.admin.core.schema import SchedulerTaskRecord
from rock.logger import init_logger

logger = init_logger(__name__)


class Phase(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    REJECTED = "Rejected"
    RATE_LIMITED = "RateLimited"
    NOT_FOUND = "NotFound"


class SchedulerTaskTable:
    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    @_retry_on_disconnect
    async def insert_tasks(self, records: list[SchedulerTaskRecord]) -> None:
        return await self._db.run(self._insert_tasks_sync, records)

    def _insert_tasks_sync(self, records: list[SchedulerTaskRecord]) -> None:
        with self._db.session_factory() as session:
            for r in records:
                session.add(r)
            session.commit()

    @_retry_on_disconnect
    async def get_tasks_by_group(self, taskset_id: str) -> list[SchedulerTaskRecord]:
        return await self._db.run(self._get_tasks_by_group_sync, taskset_id)

    def _get_tasks_by_group_sync(self, taskset_id: str) -> list[SchedulerTaskRecord]:
        with self._db.session_factory() as session:
            stmt = select(SchedulerTaskRecord).where(SchedulerTaskRecord.taskset_id == taskset_id)
            rows = session.execute(stmt).scalars().all()
            return list(rows)

    @_retry_on_disconnect
    async def update_task(self, task_id: str, **fields) -> bool:
        return await self._db.run(self._update_task_sync, task_id, fields)

    def _update_task_sync(self, task_id: str, fields: dict) -> bool:
        with self._db.session_factory() as session:
            row = session.get(SchedulerTaskRecord, task_id)
            if row is None:
                return False
            for k, v in fields.items():
                setattr(row, k, v)
            session.commit()
            return True

    @_retry_on_disconnect
    async def has_recent_task(self, task_type: str, since_epoch: float) -> bool:
        return await self._db.run(self._has_recent_task_sync, task_type, since_epoch)

    def _has_recent_task_sync(self, task_type: str, since_epoch: float) -> bool:
        with self._db.session_factory() as session:
            stmt = (
                select(SchedulerTaskRecord.task_id)
                .where(
                    SchedulerTaskRecord.task_type == task_type, SchedulerTaskRecord.creation_timestamp >= since_epoch
                )
                .limit(1)
            )
            row = session.execute(stmt).first()
            return row is not None
