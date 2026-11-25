"""Database-backed Job and Group metadata SDK."""

from rock.sdk.job.metadata.models import (
    ACTIVE_JOB_STATUSES,
    NOT_COMPLETED_JOB_STATUSES,
    UNSUCCESSFUL_JOB_STATUSES,
    GroupJobQuery,
    GroupQuery,
    JobGroupDetail,
    JobGroupMeta,
    JobGroupMode,
    JobGroupPage,
    JobGroupStatistics,
    JobGroupStatus,
    JobGroupUpdate,
    JobMeta,
    JobPage,
    JobStatus,
    JobStatusCategory,
    JobUpdate,
    JobUpdateItem,
    PageRequest,
    RunMeta,
)

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "NOT_COMPLETED_JOB_STATUSES",
    "UNSUCCESSFUL_JOB_STATUSES",
    "GroupJobQuery",
    "GroupQuery",
    "JobGroupDetail",
    "JobGroupMeta",
    "JobGroupMode",
    "JobGroupPage",
    "JobGroupStatistics",
    "JobGroupStatus",
    "JobGroupUpdate",
    "JobMeta",
    "JobMetadataRepository",
    "JobPage",
    "JobStatus",
    "JobStatusCategory",
    "JobUpdate",
    "JobUpdateItem",
    "PageRequest",
    "RunMeta",
    "JobGroupRecord",
    "JobRecord",
    "MetadataBase",
    "MetadataConflictError",
    "MetadataConstraintError",
    "MetadataNotFoundError",
    "MetadataPaginationError",
    "MetadataRepositoryError",
    "MetadataValidationError",
]


def __getattr__(name: str):
    if name == "JobMetadataRepository":
        from rock.sdk.job.metadata.repository import JobMetadataRepository

        return JobMetadataRepository
    if name in {"JobGroupRecord", "JobRecord", "MetadataBase"}:
        from rock.sdk.job.metadata import schema

        return getattr(schema, name)
    if name in {
        "MetadataConflictError",
        "MetadataConstraintError",
        "MetadataNotFoundError",
        "MetadataPaginationError",
        "MetadataRepositoryError",
        "MetadataValidationError",
    }:
        from rock.sdk.job.metadata import errors

        return getattr(errors, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
