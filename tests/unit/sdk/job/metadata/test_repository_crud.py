from uuid import uuid4

import pytest

from rock.sdk.job.metadata.errors import (
    MetadataConflictError,
    MetadataConstraintError,
    MetadataNotFoundError,
)
from rock.sdk.job.metadata.models import (
    JobGroupUpdate,
    JobStatus,
    JobUpdate,
    JobUpdateItem,
)


def test_create_and_get_group(repo, group):
    created = repo.create_group(group)

    assert repo.get_group(created.group_id) == created


def test_duplicate_job_names_get_distinct_ids(repo, group, make_job):
    repo.create_group(group)

    first = repo.create_job(make_job(group, job_name="same", task_id="t1"))
    second = repo.create_job(make_job(group, job_name="same", task_id="t2"))

    assert first.job_id != second.job_id


def test_get_job_by_id_returns_complete_metadata_for_duplicate_names(repo, group, make_job):
    repo.create_group(group)
    first = repo.create_job(make_job(group, job_name="same", task_id="t1"))
    second = repo.create_job(make_job(group, job_name="same", task_id="t2"))

    repo.update_job(
        first.job_id,
        JobUpdate(
            status="failed",
            sandbox_id="sandbox-1",
            session="rock-job-same",
            pid=123,
            exit_code=17,
            score=0.25,
            error="worker exited",
        ),
    )

    found = repo.get_job(first.job_id)

    assert found is not None
    assert found.job_id == first.job_id
    assert found.job_id != second.job_id
    assert found.namespace == "ns"
    assert found.experiment_id == "exp"
    assert found.job_name == "same"
    assert found.group_id == group.group_id
    assert found.task_id == "t1"
    assert found.job_type == "bash"
    assert found.status == JobStatus.FAILED
    assert found.sandbox_id == "sandbox-1"
    assert found.session == "rock-job-same"
    assert found.pid == 123
    assert found.exit_code == 17
    assert found.score == 0.25
    assert found.error == "worker exited"
    assert found.created_at is not None
    assert found.updated_at >= found.created_at
    assert found.started_at is None
    assert found.finished_at is None


def test_create_group_with_jobs_is_atomic(repo, group, make_job):
    jobs = [
        make_job(group, task_id="same"),
        make_job(group, task_id="same"),
    ]

    with pytest.raises(MetadataConflictError):
        repo.create_group_with_jobs(group, jobs)

    assert repo.get_group(group.group_id) is None


def test_job_must_match_group_scope(repo, group, make_job):
    repo.create_group(group)
    mismatched = make_job(group, namespace="another")

    with pytest.raises(MetadataConstraintError):
        repo.create_job(mismatched)


def test_update_job_changes_only_mutable_fields(repo, group, make_job):
    repo.create_group(group)
    job = repo.create_job(make_job(group))

    updated = repo.update_job(job.job_id, JobUpdate(status="running", pid=123))

    assert updated.status == JobStatus.RUNNING
    assert updated.pid == 123
    assert updated.job_name == job.job_name
    assert updated.updated_at >= job.updated_at


def test_update_can_explicitly_clear_nullable_value(repo, group, make_job):
    repo.create_group(group)
    job = repo.create_job(make_job(group, error="temporary"))

    updated = repo.update_job(job.job_id, JobUpdate(error=None))

    assert updated.error is None


def test_update_group(repo, group):
    repo.create_group(group)

    updated = repo.update_group(group.group_id, JobGroupUpdate(status="running"))

    assert updated.status.value == "running"


def test_batch_update_is_atomic(repo, group, make_job):
    repo.create_group(group)
    job = repo.create_job(make_job(group))

    with pytest.raises(MetadataNotFoundError):
        repo.batch_update_jobs(
            [
                JobUpdateItem(job_id=job.job_id, changes=JobUpdate(status="running")),
                JobUpdateItem(job_id=uuid4(), changes=JobUpdate(status="failed")),
            ]
        )

    assert repo.get_job(job.job_id).status == JobStatus.PLANNED


def test_get_missing_records_returns_none(repo):
    assert repo.get_job(uuid4()) is None
    assert repo.get_group(uuid4()) is None


def test_update_missing_record_raises(repo):
    with pytest.raises(MetadataNotFoundError):
        repo.update_job(uuid4(), JobUpdate(status="failed"))
