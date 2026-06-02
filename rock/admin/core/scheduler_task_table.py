"""SchedulerTaskTable: single-table CRUD for scheduler task executions.

Tasks are grouped by taskset_id (one group per API call).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import _retry_on_disconnect
from rock.admin.core.schema import SchedulerTaskRecord
from rock.logger import init_logger

logger = init_logger(__name__)

PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_SUCCEEDED = "Succeeded"
PHASE_FAILED = "Failed"
PHASE_REJECTED = "Rejected"
PHASE_RATE_LIMITED = "RateLimited"
PHASE_NOT_FOUND = "NotFound"


class SchedulerTaskTable:
    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    @_retry_on_disconnect
    async def insert_tasks(self, records: list[dict]) -> None:
        async with AsyncSession(self._db.engine) as session:
            for r in records:
                session.add(SchedulerTaskRecord(**r))
            await session.commit()

    @_retry_on_disconnect
    async def get_tasks_by_group(self, taskset_id: str) -> list[dict]:
        async with AsyncSession(self._db.engine) as session:
            stmt = select(SchedulerTaskRecord).where(SchedulerTaskRecord.taskset_id == taskset_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_dict(r) for r in rows]

    @_retry_on_disconnect
    async def update_task(self, task_id: str, **fields) -> bool:
        async with AsyncSession(self._db.engine) as session:
            row = await session.get(SchedulerTaskRecord, task_id)
            if row is None:
                return False
            for k, v in fields.items():
                setattr(row, k, v)
            await session.commit()
            return True

    @_retry_on_disconnect
    async def has_recent_task(self, task_type: str, since_epoch: float) -> bool:
        async with AsyncSession(self._db.engine) as session:
            stmt = (
                select(SchedulerTaskRecord.task_id)
                .where(SchedulerTaskRecord.task_type == task_type, SchedulerTaskRecord.creation_timestamp >= since_epoch)
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
            return row is not None


def _to_dict(row: SchedulerTaskRecord) -> dict:
    return {c.name: getattr(row, c.name) for c in SchedulerTaskRecord.__table__.columns}
