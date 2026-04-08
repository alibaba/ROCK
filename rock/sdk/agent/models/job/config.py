"""Job configuration models aligned with harbor.models.job.config.

Harbor-native fields are serialized to YAML and passed to ``harbor jobs start -c``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rock.sdk.agent.constants import USER_DEFINED_LOGS
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    OssMirrorConfig,
    RockEnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)

# ---------------------------------------------------------------------------
# RetryConfig / OrchestratorConfig
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Registry info (aligned with harbor.models.registry, field definitions only)
# ---------------------------------------------------------------------------


class OssRegistryInfo(BaseModel):
    """OSS registry, corresponds to CLI ``--registry-type oss``."""

    split: str | None = None
    revision: str | None = None
    oss_dataset_path: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None
    oss_endpoint: str | None = None
    oss_bucket: str | None = None


class RemoteRegistryInfo(BaseModel):
    """Remote registry (default GitHub), corresponds to CLI ``--registry-url``."""

    name: str | None = None
    url: str = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"


class LocalRegistryInfo(BaseModel):
    """Local registry, corresponds to CLI ``--registry-path``."""

    name: str | None = None
    path: Path


# ---------------------------------------------------------------------------
# DatasetConfig (aligned with harbor's LocalDatasetConfig / RegistryDatasetConfig)
# ---------------------------------------------------------------------------


class BaseDatasetConfig(BaseModel):
    """Common dataset fields."""

    task_names: list[str] | None = None
    exclude_task_names: list[str] | None = None
    n_tasks: int | None = None


class LocalDatasetConfig(BaseDatasetConfig):
    """Local dataset directory, corresponds to CLI ``-p/--path`` (when pointing to a dataset dir)."""

    path: Path


class RegistryDatasetConfig(BaseDatasetConfig):
    """Registry dataset, corresponds to CLI ``-d/--dataset`` + ``--registry-type``."""

    registry: OssRegistryInfo | RemoteRegistryInfo | LocalRegistryInfo
    name: str
    version: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None

    @model_validator(mode="after")
    def _infer_version_from_split(self):
        """Align with harbor CLI behavior: auto-fill version from OssRegistryInfo.split."""
        if self.version is None and isinstance(self.registry, OssRegistryInfo) and self.registry.split:
            self.version = (
                f"{self.registry.split}@{self.registry.revision}" if self.registry.revision else self.registry.split
            )
        return self


# Convenience alias
DatasetConfig = LocalDatasetConfig | RegistryDatasetConfig


class JobConfig(BaseModel):
    """Job configuration: Rock environment + Harbor-native benchmark fields.

    All Rock sandbox/lifecycle configuration lives in ``environment``.
    Harbor-native fields (agents, datasets, etc.) are serialized to YAML
    and passed to ``harbor jobs start -c``.
    """

    # ── Rock environment (Rock sandbox config + Harbor EnvironmentConfig, not serialized to Harbor YAML) ──
    environment: RockEnvironmentConfig = Field(default_factory=RockEnvironmentConfig)

    # ── Harbor native fields ──
    namespace: str | None = Field(
        default=None,
        description="Tenant isolation identifier for distinguishing resources across teams/projects",
    )
    experiment_id: str | None = Field(
        default=None,
        description="Experiment identifier",
    )
    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))
    jobs_dir: Path = Path(USER_DEFINED_LOGS) / "jobs"
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    agent_setup_timeout_multiplier: float | None = None
    environment_build_timeout_multiplier: float | None = None
    debug: bool = False
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    metrics: list[MetricConfig] = Field(default_factory=list)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list[LocalDatasetConfig | RegistryDatasetConfig] = Field(default_factory=list)
    tasks: list[TaskConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Key-value labels for organizing and filtering jobs. "
        "Example: {'step': '42', 'env': 'prod'}. "
        "Keys: [prefix/]name, lowercase, max 63 chars. "
        "Values: max 255 chars. Reserved prefix: 'harbor.io/'.",
    )

    @model_validator(mode="after")
    def _sync_experiment_id(self):
        """Validate and sync experiment_id between JobConfig and SandboxConfig.

        1. experiment_id must not be empty.
        2. If environment.experiment_id is already set, it must match.
        3. Propagate experiment_id down to environment (SandboxConfig).
        """
        if not self.experiment_id:
            raise ValueError("experiment_id must not be empty")
        env_exp = self.environment.experiment_id
        if env_exp is not None and env_exp != self.experiment_id:
            raise ValueError(
                f"experiment_id mismatch: JobConfig has '{self.experiment_id}', "
                f"but environment (SandboxConfig) has '{env_exp}'"
            )
        self.environment.experiment_id = self.experiment_id
        return self

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML for ``harbor jobs start -c``.

        Rock environment fields are excluded. Harbor environment fields
        (force_build, override_cpus, etc.) are included under ``environment``.
        """
        import yaml

        data = self.model_dump(mode="json", exclude={"environment"}, exclude_none=True)
        harbor_env = self.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str) -> JobConfig:
        """Load JobConfig from a Harbor YAML config file."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def enable_oss_mirror(
        self,
        *,
        oss_bucket: str,
        oss_access_key_id: str,
        oss_access_key_secret: str,
        oss_region: str,
        oss_endpoint: str,
    ) -> None:
        """Enable OSS artifact mirror on the environment config (credentials only).

        Set top-level ``namespace`` / ``experiment_id`` on ``JobConfig`` separately,
        or rely on ``Job`` autofill from the sandbox (see ``_autofill_sandbox_info``).
        """
        self.environment.oss_mirror = OssMirrorConfig(
            enabled=True,
            oss_bucket=oss_bucket,
            oss_access_key_id=oss_access_key_id,
            oss_access_key_secret=oss_access_key_secret,
            oss_region=oss_region,
            oss_endpoint=oss_endpoint,
        )
