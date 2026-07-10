"""Unified job metadata — written inside the sandbox, read by JobViewer.

Bash jobs write ``rock_meta.json`` via the wrapper script prologue/epilogue.
The existing ossutil upload automatically pushes it to
``artifacts/{namespace}/{experiment_id}/{job_name}/rock_meta.json``.

Harbor jobs rely on Harbor's own result.json and OSS mirror mechanism;
rock_meta.json is currently only generated for Bash jobs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig


class JobMeta(BaseModel):
    """Unified job metadata — common format for Harbor and Bash jobs."""

    schema_version: str = "1"
    job_name: str = ""
    job_type: str = ""
    status: str = ""
    run_id: str | None = None
    task_id: str | None = None
    attempt: int = 1
    status_reason: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    user_id: str | None = None
    image: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    sandbox_id: str | None = None
    session: str | None = None
    pid: int | None = None
    tmp_file: str | None = None
    script_path: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    score: float | None = None
    error: str | None = None


class RunScoreSummary(BaseModel):
    """Aggregated score summary for a full-dataset run."""

    completed: int
    failed: int
    skipped: int
    avg_score: float
    total_score: float
    pass_rate: float
    scores: dict[str, float] = Field(default_factory=dict)


class RunMeta(BaseModel):
    """Run-level metadata for single, multi, and full modes."""

    schema_version: str = "2"
    run_id: str
    mode: str = "full"
    status: str
    dataset: str | None = None
    split: str | None = None
    total_tasks: int
    pending_tasks: int
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    status_reason: str | None = None
    task_job_map: dict[str, str] = Field(default_factory=dict)
    summary: RunScoreSummary | None = None


class RunJobRef(BaseModel):
    task_id: str
    job_name: str


class RunJobStatus(BaseModel):
    task_id: str
    job_name: str
    status: str = "unknown"
    sandbox_id: str | None = None
    score: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


def render_meta_json(config: JobConfig, *, job_type: str, status: str = "running") -> str:
    """Render a rock_meta.json string from a JobConfig.

    All fields are resolved at Python render time (no shell placeholders).
    Shell-side timing and exit code are handled separately by the wrapper scripts.
    """
    user_id = getattr(config.environment, "user_id", None)
    if not user_id:
        import os

        user_id = os.environ.get("ROCK_USER_ID")

    image = getattr(config.environment, "image", None)

    meta = JobMeta(
        job_name=config.job_name or "",
        job_type=job_type,
        status=status,
        namespace=config.namespace,
        experiment_id=config.experiment_id,
        user_id=user_id,
        image=image,
        labels=config.labels,
    )
    return meta.model_dump_json(indent=2)
