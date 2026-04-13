"""Config hierarchy for the Job system.

JobConfig          — base config with shared fields for all job types
BashJobConfig      — simple script execution
HarborJobConfig    — full Harbor benchmark job (agents, verifier, tasks, …)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from rock.sdk.agent.constants import USER_DEFINED_LOGS
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    RockEnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)


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


class HarborJobConfig(JobConfig):
    """Config for a full Harbor benchmark job."""

    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list = Field(default_factory=list)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    tasks: list[TaskConfig] = Field(default_factory=list)
    metrics: list = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    jobs_dir: Path = Path(USER_DEFINED_LOGS) / "jobs"
    debug: bool = False

    # Exclude Rock-level fields when serializing to Harbor YAML
    _ROCK_FIELDS: ClassVar[set[str]] = {
        "environment",
        "job_name",
        "namespace",
        "experiment_id",
        "labels",
        "auto_stop",
        "setup_commands",
        "file_uploads",
        "env",
        "timeout",
    }

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML, excluding Rock-level fields."""
        import yaml

        data = self.model_dump(mode="json", exclude=self._ROCK_FIELDS, exclude_none=True)
        harbor_env = self.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str) -> HarborJobConfig:
        """Load HarborJobConfig from a YAML file."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
