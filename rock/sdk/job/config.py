"""Config hierarchy for the Job system.

JobEnvironmentConfig — sandbox config + job-level environment fields
JobConfig            — base config with shared job-scheduling fields
BashJobConfig        — simple script execution

Harbor's HarborJobConfig lives in rock.sdk.bench.models.job.config.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from rock.sdk.sandbox.config import SandboxConfig


class JobEnvironmentConfig(SandboxConfig):
    """Job environment config — sandbox base fields + job-level environment fields."""

    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]",
    )
    auto_stop: bool = False
    env: dict[str, str] = Field(default_factory=dict)


class JobConfig(BaseModel):
    """Base config — shared fields for all job types."""

    environment: JobEnvironmentConfig = Field(default_factory=JobEnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    timeout: int = 3600


class BashJobConfig(JobConfig):
    """Config for a simple bash script job."""

    script: str | None = None
    script_path: str | None = None
