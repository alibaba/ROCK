"""Read access for sandbox template metadata."""

from typing import TypedDict

from sqlalchemy import or_, select

from rock.admin.core.db_provider import DatabaseProvider, retry_on_disconnect
from rock.admin.core.schema import TemplateRecord

_READY_STATUS = "READY"


class ReadyTemplate(TypedDict):
    image: str | None
    cpu_count: int
    memory_mb: int
    disk_size_mb: int


class TemplateTable:
    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    @retry_on_disconnect
    async def get_ready_fiber_pool_id(self, template_id: str) -> str | None:
        return await self._db.run(self._get_ready_fiber_pool_id_sync, template_id)

    def _get_ready_fiber_pool_id_sync(self, template_id: str) -> str | None:
        with self._db.session_factory() as session:
            stmt = select(TemplateRecord.fiber_pool_id).where(
                TemplateRecord.template_id == template_id,
                TemplateRecord.status == _READY_STATUS,
            )
            return session.execute(stmt).scalar_one_or_none()

    @retry_on_disconnect
    async def get_ready_template(self, template_id: str) -> ReadyTemplate | None:
        return await self._db.run(self._get_ready_template_sync, template_id)

    def _get_ready_template_sync(self, template_id: str) -> ReadyTemplate | None:
        with self._db.session_factory() as session:
            stmt = (
                select(
                    TemplateRecord.image,
                    TemplateRecord.cpu_count,
                    TemplateRecord.memory_mb,
                    TemplateRecord.disk_size_mb,
                )
                .where(
                    or_(TemplateRecord.template_id == template_id, TemplateRecord.image == template_id),
                    TemplateRecord.status == _READY_STATUS,
                )
                .order_by((TemplateRecord.template_id == template_id).desc())
                .limit(1)
            )
            row = session.execute(stmt).one_or_none()
            if row is None:
                return None
            return {
                "image": row.image,
                "cpu_count": row.cpu_count,
                "memory_mb": row.memory_mb,
                "disk_size_mb": row.disk_size_mb,
            }
