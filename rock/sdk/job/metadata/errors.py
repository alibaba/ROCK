"""Stable SDK exceptions for metadata persistence."""


class MetadataRepositoryError(RuntimeError):
    """Base class for errors exposed by the metadata repository."""


class MetadataConflictError(MetadataRepositoryError):
    """A primary key or business uniqueness constraint was violated."""


class MetadataNotFoundError(MetadataRepositoryError):
    """A record required by a write or aggregate operation does not exist."""


class MetadataConstraintError(MetadataRepositoryError):
    """The requested records violate a relationship or database constraint."""


class MetadataValidationError(MetadataRepositoryError):
    """Repository-level input validation failed."""


class MetadataPaginationError(MetadataRepositoryError):
    """A pagination cursor is invalid or incompatible."""
