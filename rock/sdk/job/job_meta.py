"""Compatibility import for the database metadata repository."""

from rock.sdk.job.metadata.repository import JobMetadataRepository

JobMetaRepository = JobMetadataRepository

__all__ = ["JobMetaRepository", "JobMetadataRepository"]
