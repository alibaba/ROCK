"""Response models for Tinker backend server jobs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ServerJobStatus = Literal["submitted", "pending", "running", "succeeded", "failed", "stopped"]


class ServerJobSubmitResponse(BaseModel):
    job_id: str
    platform: str
    status: ServerJobStatus
    submitted_at: str
    base_url: str | None = None


class ServerJobStatusResponse(BaseModel):
    job_id: str
    platform: str
    status: ServerJobStatus
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    base_url: str | None = None
    log_dir: str | None = None


class ServerJobStopResponse(BaseModel):
    job_id: str
    status: str
    message: str | None = None


class ServerJobLogsResponse(BaseModel):
    job_id: str
    log_path: str | None = None
    log_url: str | None = None
    expires_at: str | None = None
    log_size_bytes: int | None = None
