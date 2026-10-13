"""Compatibility import for the database metadata repository."""

from rock.sdk.job.metadata.repository import JobMetadataRepository

RunMetaRepository = JobMetadataRepository

__all__ = ["JobMetadataRepository", "RunMetaRepository"]
