"""Config hierarchy for the Job system.

JobConfig    — base config with shared job-scheduling fields
BashJobConfig — simple script execution

Environment config lives in rock.sdk.envhub.config.EnvironmentConfig.
Harbor's HarborJobConfig lives in rock.sdk.bench.models.job.config.
"""

from __future__ import annotations

from datetime import datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rock.logger import init_logger
from rock.sdk.envhub import EnvironmentConfig

logger = init_logger(__name__)


class JobConfig(BaseModel):
    """Base config — shared fields for all job types."""

    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    timeout: int = 7200

    @model_validator(mode="after")
    def _sync_experiment_id(self) -> JobConfig:
        """When both experiment_id fields are set and differ, JobConfig.experiment_id takes priority."""
        if (
            self.experiment_id is not None
            and self.environment.experiment_id is not None
            and self.experiment_id != self.environment.experiment_id
        ):
            logger.warning(
                "experiment_id conflict: JobConfig has '%s', environment has '%s'. "
                "Using JobConfig.experiment_id and overriding environment.experiment_id.",
                self.experiment_id,
                self.environment.experiment_id,
            )
            self.environment.experiment_id = self.experiment_id
        return self

    @classmethod
    def from_yaml(cls, path: str) -> JobConfig:
        """Load a job config from YAML.

        When called on the base class (``JobConfig.from_yaml``), the job type is
        auto-detected by trying each concrete subclass in order:

        1. ``HarborJobConfig`` — tried first (requires ``experiment_id``)
        2. ``BashJobConfig``   — tried second (all fields optional)

        Both models use ``extra="forbid"``, so any field that belongs to the
        other type causes a ``ValidationError`` and the attempt is skipped.
        If both fail the combined ``ValidationError`` details are surfaced.

        When called directly on a subclass (e.g. ``BashJobConfig.from_yaml``),
        no auto-detection is performed.
        """
        with open(path) as f:
            data = yaml.safe_load(f)

        if cls is not JobConfig:
            # Called as BashJobConfig.from_yaml() or HarborJobConfig.from_yaml() —
            # respect the explicit class, skip auto-detection.
            return cls(**data)

        # Lazy import to avoid circular dependency:
        # rock.sdk.bench.models.job.config → rock.sdk.job.config
        from rock.sdk.bench.models.job.config import HarborJobConfig

        harbor_error: ValidationError | None = None
        bash_error: ValidationError | None = None

        try:
            return HarborJobConfig.model_validate(data)
        except (ValidationError, ValueError) as exc:
            harbor_error = exc

        try:
            return BashJobConfig.model_validate(data)
        except (ValidationError, ValueError) as exc:
            bash_error = exc

        raise ValueError(
            "YAML does not match any known job type.\n"
            f"  As HarborJobConfig: {harbor_error}\n"
            f"  As BashJobConfig:   {bash_error}"
        )


class BashJobConfig(JobConfig):
    """Config for a simple bash script job."""

    model_config = ConfigDict(extra="forbid")

    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))
    script: str | None = None
    script_path: str | None = None
