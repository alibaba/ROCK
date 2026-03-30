"""Job configuration models aligned with harbor.models.job.config.

Harbor-native fields are serialized to YAML and passed to ``harbor jobs start -c``.
Rock environment fields live in RockEnvironmentConfig (unified SandboxConfig + HarborEnvConfig).
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
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.agent.models.trial.config import (
    EnvironmentConfig as _HarborEnvConfig,
)
from rock.sdk.sandbox.config import SandboxConfig

# ---------------------------------------------------------------------------
# RockEnvironmentConfig — unified environment config
# ---------------------------------------------------------------------------


class RockEnvironmentConfig(SandboxConfig, _HarborEnvConfig):
    """Unified Rock environment config.

    Combines sandbox lifecycle fields (image, memory, cpus, ...) with
    harbor environment fields (force_build, override_cpus, ...) in a single
    flat block. Rock-specific fields are stripped when serializing to Harbor
    YAML via to_harbor_environment().
    """

    # Env vars injected into the sandbox bash session.
    # Harbor runs as a subprocess and inherits them naturally.
    # Overrides the env field from the parent EnvironmentConfig.
    env: dict[str, str] = Field(default_factory=dict)

    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]",
    )
    auto_stop: bool = False

    def to_harbor_environment(self) -> dict:
        """Return only harbor-native environment fields, discarding Rock-only fields.

        env is excluded: it's injected into the sandbox session instead,
        harbor inherits it naturally as a subprocess.
        Uses model_validate upcast — unknown fields (Rock-only) are silently ignored.
        """
        harbor = _HarborEnvConfig.model_validate(self.model_dump(mode="json", exclude={"env"}))
        return harbor.model_dump(mode="json", exclude_none=True)


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

    # ── Rock environment (sandbox + lifecycle) ──
    environment: RockEnvironmentConfig = Field(default_factory=RockEnvironmentConfig)

    # ── Harbor native fields ──
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
    def from_yaml(cls, path: str, **overrides) -> JobConfig:
        """Load JobConfig from a Harbor YAML config file.

        Args:
            path: Path to the YAML file.
            **overrides: Fields to override. Pass ``environment`` as a dict
                to merge into the loaded environment block, e.g.:
                ``from_yaml(path, environment={"setup_commands": ["pip install x"]})``
        """
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)

        # Merge environment overrides into the loaded environment block
        if "environment" in overrides:
            env_override = overrides.pop("environment")
            existing = data.get("environment") or {}
            if isinstance(env_override, dict):
                existing.update(env_override)
            elif hasattr(env_override, "model_dump"):
                existing.update(env_override.model_dump(exclude_none=True))
            data["environment"] = existing

        data.update(overrides)
        return cls(**data)
