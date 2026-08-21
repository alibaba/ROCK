from collections.abc import Callable

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rock.sdk.job.metadata.models import JobGroupMeta, JobMeta, JobStatus
from rock.sdk.job.metadata.repository import JobMetadataRepository
from rock.sdk.job.metadata.schema import MetadataBase


@pytest.fixture
def engine() -> Engine:
    database = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(database, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    MetadataBase.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def repo(engine: Engine) -> JobMetadataRepository:
    return JobMetadataRepository(sessionmaker(engine, expire_on_commit=False))


@pytest.fixture
def make_group() -> Callable[..., JobGroupMeta]:
    def factory(**overrides) -> JobGroupMeta:
        values = {
            "namespace": "ns",
            "experiment_id": "exp",
            "mode": "full",
            "status": "planning",
        }
        values.update(overrides)
        return JobGroupMeta(**values)

    return factory


@pytest.fixture
def group(make_group) -> JobGroupMeta:
    return make_group()


@pytest.fixture
def make_job() -> Callable[..., JobMeta]:
    def factory(group: JobGroupMeta | None = None, **overrides) -> JobMeta:
        values = {
            "namespace": group.namespace if group else "ns",
            "experiment_id": group.experiment_id if group else "exp",
            "job_name": "job",
            "group_id": group.group_id if group else None,
            "task_id": "task-1" if group else None,
            "job_type": "bash",
            "status": JobStatus.PLANNED,
        }
        values.update(overrides)
        return JobMeta(**values)

    return factory
