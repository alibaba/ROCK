"""Read access for sandbox template metadata."""

from sqlalchemy import select

from rock.admin.core.db_provider import DatabaseProvider, retry_on_disconnect
from rock.admin.core.schema import TemplateRecord

_READY_STATUS = "READY"


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
