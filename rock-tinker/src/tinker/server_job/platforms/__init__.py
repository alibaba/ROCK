"""ServerJob provider factory."""

from __future__ import annotations

from tinker.server_job.config_types import (
    LocalServerJobConfig,
    ServerJobConfigUnion,
)
from tinker.server_job.platforms.base import ServerJobHandle, ServerJobProvider
from tinker.server_job.platforms.local import LocalServerJobProvider


def create_server_job_provider(config: ServerJobConfigUnion) -> ServerJobProvider:
    if isinstance(config, LocalServerJobConfig):
        return LocalServerJobProvider(config)
    raise ValueError(f"Unsupported ServerJob config: {config!r}")


__all__ = [
    "ServerJobHandle",
    "ServerJobProvider",
    "LocalServerJobProvider",
    "create_server_job_provider",
]
