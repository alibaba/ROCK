"""Config hierarchy for the Job system.

JobConfig      — base config with shared fields for all job types
BashJobConfig  — simple script execution

Harbor's JobConfig lives in rock.sdk.agent.models.job.config and inherits JobConfig.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from rock.sdk.bench.models.trial.config import RockEnvironmentConfig


class JobConfig(BaseModel):
    """Base config — shared fields for all job types."""

    environment: RockEnvironmentConfig = Field(default_factory=RockEnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    auto_stop: bool = False
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 3600


class BashJobConfig(JobConfig):
    """Config for a simple bash script job."""

    script: str | None = None
    script_path: str | None = None
