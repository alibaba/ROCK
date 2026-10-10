"""Provider interfaces for Tinker backend server jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from tinker.server_job.response_types import (
    ServerJobLogsResponse,
    ServerJobStatusResponse,
    ServerJobStopResponse,
)


class ServerJobHandle(BaseModel):
    job_id: str
    platform: str
    provider_job_id: str | None = None
    ingress_id: str | None = None
    base_url: str | None = None
    log_dir: str | None = None
    process_pid: int | None = None


class ServerJobProvider(Protocol):
    async def submit(self) -> ServerJobHandle: ...

    async def attach(self, job_id: str) -> ServerJobHandle: ...

    async def status(self, handle: ServerJobHandle) -> ServerJobStatusResponse: ...

    async def stop(self, handle: ServerJobHandle, *, force_after_seconds: float | None = None) -> ServerJobStopResponse: ...

    async def resolve_log_path(self, handle: ServerJobHandle, *, kind: str = "cookbook", runtime_id: str | None = None) -> Path: ...

    async def download_logs(self, handle: ServerJobHandle, output_path: Path | None) -> ServerJobLogsResponse: ...
