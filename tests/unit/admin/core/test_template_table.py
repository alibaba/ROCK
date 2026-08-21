import datetime

import pytest
from sqlalchemy import DateTime
from sqlalchemy.exc import IntegrityError

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
            image="registry.example.com/rock/template-ready:latest",
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
    assert await table.get_ready_template("template-ready") == {
        "image": "registry.example.com/rock/template-ready:latest",
        "cpu_count": 4,
        "memory_mb": 8192,
        "disk_size_mb": 20480,
    }


async def test_get_ready_template_accepts_image_as_identifier(db_provider):
    table = TemplateTable(db_provider)
    image = "registry.example.com/rock/template-by-image:latest"
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-by-image",
            image=image,
            os_type="linux",
            status="READY",
            fiber_pool_id="pool-by-image",
            cpu_count=8,
            memory_mb=16384,
            disk_size_mb=40960,
            created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        ),
    )

    assert await table.get_ready_template(image) == {
        "image": image,
        "cpu_count": 8,
        "memory_mb": 16384,
        "disk_size_mb": 40960,
    }
    assert await table.get_ready_fiber_pool_id(image) == "pool-by-image"


async def test_get_ready_template_supports_null_image(db_provider):
    table = TemplateTable(db_provider)
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-without-image",
            image=None,
            os_type="linux",
            status="READY",
            cpu_count=2,
            memory_mb=4096,
            disk_size_mb=10240,
            created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        ),
    )
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="another-template-without-image",
            image=None,
            os_type="linux",
            status="READY",
            cpu_count=4,
            memory_mb=8192,
            disk_size_mb=20480,
            created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        ),
    )

    assert await table.get_ready_template("template-without-image") == {
        "image": None,
        "cpu_count": 2,
        "memory_mb": 4096,
        "disk_size_mb": 10240,
    }
    assert await table.get_ready_template("another-template-without-image") is not None


async def test_get_ready_template_prefers_template_id_over_image(db_provider):
    table = TemplateTable(db_provider)
    timestamps = {
        "created_at": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        "updated_at": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
    }
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="shared-identifier",
            image="registry.example.com/rock/by-id:latest",
            fiber_pool_id="pool-by-id",
            os_type="linux",
            status="READY",
            cpu_count=2,
            memory_mb=4096,
            disk_size_mb=10240,
            **timestamps,
        ),
    )
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="another-template",
            image="shared-identifier",
            fiber_pool_id="pool-by-image",
            os_type="linux",
            status="READY",
            cpu_count=8,
            memory_mb=16384,
            disk_size_mb=40960,
            **timestamps,
        ),
    )

    assert await table.get_ready_template("shared-identifier") == {
        "image": "registry.example.com/rock/by-id:latest",
        "cpu_count": 2,
        "memory_mb": 4096,
        "disk_size_mb": 10240,
    }
    assert await table.get_ready_fiber_pool_id("shared-identifier") == "pool-by-id"


async def test_template_image_must_be_unique(db_provider):
    image = "registry.example.com/rock/shared:latest"
    timestamps = {
        "created_at": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
        "updated_at": datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc),
    }
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-first",
            image=image,
            os_type="linux",
            status="READY",
            cpu_count=2,
            memory_mb=4096,
            disk_size_mb=10240,
            **timestamps,
        ),
    )

    with pytest.raises(IntegrityError):
        await _insert_template(
            db_provider,
            TemplateRecord(
                template_id="template-second",
                image=image,
                os_type="linux",
                status="READY",
                cpu_count=4,
                memory_mb=8192,
                disk_size_mb=20480,
                **timestamps,
            ),
        )


@pytest.mark.parametrize("status", ["PENDING", "CREATING", "FAILED"])
async def test_get_fiber_pool_id_ignores_non_ready_template(db_provider, status):
    table = TemplateTable(db_provider)
    await _insert_template(
        db_provider,
        TemplateRecord(
            template_id="template-pending",
            image="registry.example.com/rock/template-pending:latest",
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
    assert await table.get_ready_template("template-pending") is None
