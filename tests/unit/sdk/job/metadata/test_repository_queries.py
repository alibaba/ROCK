import pytest

from rock.sdk.job.metadata.errors import (
    MetadataNotFoundError,
    MetadataValidationError,
)
from rock.sdk.job.metadata.models import (
    GroupJobQuery,
    GroupQuery,
    JobStatus,
    JobStatusCategory,
)


@pytest.fixture
def seeded(repo, group, make_job):
    repo.create_group(group)
    jobs = [
        make_job(group, task_id="planned", status="planned", job_type="bash"),
        make_job(group, task_id="running", status="running", job_type="harbor"),
        make_job(group, task_id="completed", status="completed", score=0.5),
        make_job(group, task_id="failed", status="failed", score=1.0),
    ]
    for job in jobs:
        repo.create_job(job)
    return repo, group


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (JobStatusCategory.ALL, {"planned", "running", "completed", "failed"}),
        (JobStatusCategory.ACTIVE, {"planned", "running"}),
        (JobStatusCategory.COMPLETED, {"completed"}),
        (JobStatusCategory.UNSUCCESSFUL, {"failed"}),
        (JobStatusCategory.NOT_COMPLETED, {"planned", "running", "failed"}),
    ],
)
def test_list_group_jobs_filters_categories(seeded, category, expected):
    repo, group = seeded

    page = repo.list_group_jobs(group.group_id, GroupJobQuery(category=category))

    assert {job.status.value for job in page.items} == expected


def test_list_group_jobs_supports_exact_statuses(seeded):
    repo, group = seeded

    page = repo.list_group_jobs(
        group.group_id,
        GroupJobQuery(statuses={JobStatus.FAILED, JobStatus.RUNNING}),
    )

    assert {job.status for job in page.items} == {JobStatus.FAILED, JobStatus.RUNNING}


def test_list_group_jobs_filters_task_and_type(seeded):
    repo, group = seeded

    page = repo.list_group_jobs(
        group.group_id,
        GroupJobQuery(task_ids={"running", "completed"}, job_type="harbor"),
    )

    assert [job.task_id for job in page.items] == ["running"]


def test_list_namespace_groups_filters_experiment(repo, make_group):
    wanted = repo.create_group(make_group(namespace="ns", experiment_id="e1"))
    repo.create_group(make_group(namespace="ns", experiment_id="e2"))
    repo.create_group(make_group(namespace="other", experiment_id="e1"))

    page = repo.list_namespace_groups("ns", GroupQuery(experiment_id="e1"))

    assert [item.group_id for item in page.items] == [wanted.group_id]


def test_list_namespace_groups_rejects_empty_namespace(repo):
    with pytest.raises(MetadataValidationError):
        repo.list_namespace_groups("")


def test_group_statistics_counts_statuses_and_scores(seeded):
    repo, group = seeded

    stats = repo.get_group_statistics(group.group_id)

    assert stats.total_jobs == 4
    assert stats.active_jobs == 2
    assert stats.completed_jobs == 1
    assert stats.failed_jobs == 1
    assert stats.cancelled_jobs == 0
    assert stats.unrecoverable_jobs == 0
    assert stats.scored_jobs == 2
    assert stats.total_score == pytest.approx(1.5)
    assert stats.avg_score == pytest.approx(0.75)
    assert stats.min_score == pytest.approx(0.5)
    assert stats.max_score == pytest.approx(1.0)


def test_group_detail_combines_group_statistics_and_filtered_jobs(seeded):
    repo, group = seeded

    detail = repo.get_group_detail(
        group.group_id,
        GroupJobQuery(category=JobStatusCategory.UNSUCCESSFUL),
    )

    assert detail.group.group_id == group.group_id
    assert detail.statistics.total_jobs == 4
    assert [job.status for job in detail.jobs.items] == [JobStatus.FAILED]


def test_group_queries_require_existing_group(repo):
    from uuid import uuid4

    missing = uuid4()
    with pytest.raises(MetadataNotFoundError):
        repo.list_group_jobs(missing)
    with pytest.raises(MetadataNotFoundError):
        repo.get_group_statistics(missing)
