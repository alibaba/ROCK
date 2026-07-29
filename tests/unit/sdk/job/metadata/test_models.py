from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from rock.sdk.job.metadata.models import (
    GroupJobQuery,
    JobGroupMeta,
    JobGroupUpdate,
    JobMeta,
    JobStatus,
    JobStatusCategory,
    JobUpdate,
    PageRequest,
)


def test_job_meta_generates_unique_uuid_for_non_unique_names():
    common = {
        "namespace": "ns",
        "experiment_id": "exp",
        "job_name": "same",
        "job_type": "bash",
        "status": JobStatus.PLANNED,
    }

    first = JobMeta(**common)
    second = JobMeta(**common)

    assert isinstance(first.job_id, UUID)
    assert isinstance(second.job_id, UUID)
    assert first.job_id != second.job_id


def test_group_job_query_rejects_category_and_statuses_together():
    with pytest.raises(ValidationError):
        GroupJobQuery(
            category=JobStatusCategory.ACTIVE,
            statuses={JobStatus.FAILED},
        )


@pytest.mark.parametrize("page_size", [0, 1001])
def test_page_request_limits_page_size(page_size: int):
    with pytest.raises(ValidationError):
        PageRequest(page_size=page_size)


def test_group_meta_uses_timezone_aware_datetimes():
    group = JobGroupMeta(
        namespace="ns",
        experiment_id="exp",
        mode="full",
        status="planning",
        created_at=datetime.now(timezone.utc),
    )
    assert group.created_at.tzinfo is not None


def test_metadata_rejects_naive_datetimes():
    with pytest.raises(ValidationError):
        JobGroupMeta(
            namespace="ns",
            experiment_id="exp",
            mode="full",
            status="planning",
            created_at=datetime.now(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        lambda: JobUpdate(status=None),
        lambda: JobGroupUpdate(status=None),
    ],
)
def test_update_rejects_explicitly_clearing_required_status(changes):
    with pytest.raises(ValidationError):
        changes()
