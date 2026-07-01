"""Unified job metadata — written inside the sandbox, read by JobViewer.

Both Harbor and Bash jobs write ``rock_meta.json`` to the job output
directory. The existing OSS upload mechanisms (ossutil for Bash,
Harbor's _oss_mirror_job_root_files for Harbor) automatically push it
to ``artifacts/{namespace}/{experiment_id}/{job_name}/rock_meta.json``.
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
    namespace: str | None = None
    experiment_id: str | None = None
    user_id: str | None = None
    image: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None


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
