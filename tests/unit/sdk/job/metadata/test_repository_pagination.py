import base64
from datetime import datetime, timezone

import pytest

from rock.sdk.job.metadata.errors import MetadataPaginationError
from rock.sdk.job.metadata.models import PageRequest


def test_group_jobs_can_return_all_without_pagination(repo, group, make_job):
    repo.create_group(group)
    for index in range(3):
        repo.create_job(make_job(group, task_id=str(index)))

    page = repo.list_group_jobs(group.group_id)

    assert len(page.items) == page.total == 3
    assert page.next_cursor is None


def test_group_jobs_cursor_has_no_duplicates(repo, group, make_job):
    repo.create_group(group)
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(5):
        repo.create_job(make_job(group, task_id=str(index), created_at=same_time))

    first = repo.list_group_jobs(group.group_id, pagination=PageRequest(page_size=2))
    second = repo.list_group_jobs(
        group.group_id,
        pagination=PageRequest(page_size=2, cursor=first.next_cursor),
    )
    third = repo.list_group_jobs(
        group.group_id,
        pagination=PageRequest(page_size=2, cursor=second.next_cursor),
    )

    ids = [job.job_id for page in (first, second, third) for job in page.items]
    assert len(ids) == len(set(ids)) == 5
    assert first.total == second.total == third.total == 5
    assert third.next_cursor is None


def test_namespace_group_cursor_has_no_duplicates(repo, make_group):
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(3):
        repo.create_group(make_group(experiment_id=str(index), created_at=same_time))

    first = repo.list_namespace_groups("ns", pagination=PageRequest(page_size=2))
    second = repo.list_namespace_groups(
        "ns",
        pagination=PageRequest(page_size=2, cursor=first.next_cursor),
    )

    ids = [group.group_id for page in (first, second) for group in page.items]
    assert len(ids) == len(set(ids)) == 3


def test_invalid_cursor_raises(repo, group):
    repo.create_group(group)

    with pytest.raises(MetadataPaginationError):
        repo.list_group_jobs(
            group.group_id,
            pagination=PageRequest(cursor="not-a-cursor"),
        )


def test_non_utf8_cursor_raises_stable_pagination_error(repo, group):
    repo.create_group(group)
    cursor = base64.urlsafe_b64encode(b"\xff\xfe").decode()

    with pytest.raises(MetadataPaginationError):
        repo.list_group_jobs(group.group_id, pagination=PageRequest(cursor=cursor))
