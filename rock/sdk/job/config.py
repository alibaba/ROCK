"""Config hierarchy for the Job system.

JobConfig    — base config with shared job-scheduling fields
BashJobConfig — simple script execution

Environment config lives in rock.sdk.envhub.config.EnvironmentConfig.
Harbor's HarborJobConfig lives in rock.sdk.bench.models.job.config.
"""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field

from rock.sdk.envhub import EnvironmentConfig


class JobConfig(BaseModel):
    """Base config — shared fields for all job types."""

    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    timeout: int = 3600

    @classmethod
    def from_yaml(cls, path: str) -> JobConfig:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


class BashJobConfig(JobConfig):
    """Config for a simple bash script job."""

    script: str | None = None
    script_path: str | None = None
