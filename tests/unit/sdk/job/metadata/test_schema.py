from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from rock.sdk.job.metadata.schema import JobGroupRecord, JobRecord


def test_postgresql_schema_uses_native_uuid():
    group_ddl = str(CreateTable(JobGroupRecord.__table__).compile(dialect=postgresql.dialect()))
    job_ddl = str(CreateTable(JobRecord.__table__).compile(dialect=postgresql.dialect()))

    assert "group_id UUID NOT NULL" in group_ddl
    assert "job_id UUID NOT NULL" in job_ddl
    assert "job_group_metadata" in group_ddl
    assert "job_metadata" in job_ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in group_ddl
    assert "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in job_ddl


def test_schema_contains_expected_indexes():
    indexes = JobGroupRecord.__table__.indexes | JobRecord.__table__.indexes
    names = {index.name for index in indexes}

    assert "ix_job_group_namespace_created" in names
    assert "uq_job_group_task" in names
    assert "ix_job_group_status" in names
