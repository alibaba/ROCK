from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)


class RetryConfig(BaseModel):
    max_retries: int = Field(default=0, ge=0)
    include_exceptions: set[str] | None = None
    exclude_exceptions: set[str] | None = Field(
        default_factory=lambda: {
            "AgentTimeoutError",
            "VerifierTimeoutError",
            "RewardFileNotFoundError",
            "RewardFileEmptyError",
            "VerifierOutputParseError",
        }
    )
    wait_multiplier: float = 1.0
    min_wait_sec: float = 1.0
    max_wait_sec: float = 60.0


class OrchestratorConfig(BaseModel):
    type: OrchestratorType = OrchestratorType.LOCAL
    n_concurrent_trials: int = 4
    quiet: bool = False
    retry: RetryConfig = Field(default_factory=RetryConfig)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class DatasetConfig(BaseModel):
    """Simplified dataset config, compatible with Harbor YAML datasets field.

    Merges LocalDatasetConfig and RegistryDatasetConfig into one class —
    only field definitions for YAML serialization, no runtime methods.
    """

    # Common fields (from BaseDatasetConfig)
    task_names: list[str] | None = None
    exclude_task_names: list[str] | None = None
    n_tasks: int | None = None

    # Local dataset (from LocalDatasetConfig)
    path: Path | None = None

    # Registry dataset (from RegistryDatasetConfig)
    name: str | None = None
    version: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None
    registry_url: str | None = None
    registry_path: Path | None = None


class JobConfig(BaseModel):
    """Job configuration combining Harbor-native fields with Rock extensions.

    Harbor-native fields are serialized to YAML and passed to `harbor jobs start -c`.
    Rock extension fields control sandbox lifecycle.
    """

    # ── Rock extension fields ──
    sandbox: Any | None = None
    setup_commands: list[str] = Field(default_factory=list)
    result_file: str = ""
    collect_trajectory: bool = False
    auto_start_sandbox: bool = True
    auto_stop_sandbox: bool = False

    # ── Harbor native fields ──
    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))
    jobs_dir: Path = Path("jobs")
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    agent_setup_timeout_multiplier: float | None = None
    environment_build_timeout_multiplier: float | None = None
    debug: bool = False
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    metrics: list[MetricConfig] = Field(default_factory=list)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list[DatasetConfig] = Field(default_factory=list)
    tasks: list[TaskConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)

    # ── Rock extension field names (excluded from Harbor YAML) ──
    _rock_fields: ClassVar[set[str]] = {
        "sandbox",
        "setup_commands",
        "result_file",
        "collect_trajectory",
        "auto_start_sandbox",
        "auto_stop_sandbox",
    }

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML string.

        Excludes Rock extension fields and None values so the output
        can be loaded by `harbor jobs start -c`.
        """
        import yaml

        data = self.model_dump(mode="json", exclude=self._rock_fields, exclude_none=True)
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str, **overrides) -> JobConfig:
        """Load JobConfig from a Harbor YAML config file.

        Args:
            path: Path to the YAML file.
            **overrides: Additional fields to set (e.g., sandbox, setup_commands).
        """
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        data.update(overrides)
        return cls(**data)
