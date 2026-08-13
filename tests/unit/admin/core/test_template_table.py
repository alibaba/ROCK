import datetime

import pytest
from sqlalchemy import DateTime

from rock.admin.core.schema import TemplateRecord
from rock.admin.core.template_table import TemplateTable


def test_template_timestamp_columns_are_timezone_aware():
    for column_name in ("created_at", "updated_at"):
        column_type = TemplateRecord.__table__.columns[column_name].type
        assert isinstance(column_type, DateTime)
        assert column_type.timezone is True


async def _insert_template(db_provider, record: TemplateRecord) -> None:
    def insert() -> None:
        with db_provider.session_factory() as session:
            session.add(record)
            session.commit()

    await db_provider.run(insert)


async def test_get_fiber_pool_id_returns_ready_template_pool(db_provider):
    table = TemplateTable(db_provider)
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-ready",
            os_type="linux",
            status="READY",
            fiber_pool_id="pool-from-db",
            cpu_count=4,
            memory_mb=8192,
            disk_size_mb=20480,
            created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        ),
    )

    assert await table.get_ready_fiber_pool_id("template-ready") == "pool-from-db"
    assert await table.get_ready_resource_spec("template-ready") == {
        "cpu_count": 4,
        "memory_mb": 8192,
        "disk_size_mb": 20480,
    }


@pytest.mark.parametrize("status", ["PENDING", "CREATING", "FAILED"])
async def test_get_fiber_pool_id_ignores_non_ready_template(db_provider, status):
    table = TemplateTable(db_provider)
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-pending",
            os_type="linux",
            status=status,
            fiber_pool_id="pool-not-ready",
            cpu_count=4,
            memory_mb=8192,
            disk_size_mb=20480,
            created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        ),
    )

    assert await table.get_ready_fiber_pool_id("template-pending") is None
    assert await table.get_ready_resource_spec("template-pending") is None
