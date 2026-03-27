from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.sandbox.config import SandboxConfig


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
# Registry info（对齐 harbor.models.registry，仅字段定义）
# ---------------------------------------------------------------------------


class OssRegistryInfo(BaseModel):
    """OSS registry，对应 CLI --registry-type oss"""

    split: str | None = None
    revision: str | None = None
    oss_dataset_path: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None
    oss_endpoint: str | None = None
    oss_bucket: str | None = None


class RemoteRegistryInfo(BaseModel):
    """远程 registry（默认 GitHub），对应 CLI --registry-url"""

    name: str | None = None
    url: str = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"


class LocalRegistryInfo(BaseModel):
    """本地 registry，对应 CLI --registry-path"""

    name: str | None = None
    path: Path


# ---------------------------------------------------------------------------
# DatasetConfig（对齐 harbor 的 LocalDatasetConfig / RegistryDatasetConfig）
# ---------------------------------------------------------------------------


class BaseDatasetConfig(BaseModel):
    """数据集通用字段。"""

    task_names: list[str] | None = None
    exclude_task_names: list[str] | None = None
    n_tasks: int | None = None


class LocalDatasetConfig(BaseDatasetConfig):
    """本地数据集目录，对应 CLI -p/--path（指向 dataset 目录时）。"""

    path: Path


class RegistryDatasetConfig(BaseDatasetConfig):
    """Registry 数据集，对应 CLI -d/--dataset + --registry-type。"""

    registry: OssRegistryInfo | RemoteRegistryInfo | LocalRegistryInfo
    name: str
    version: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None

    @model_validator(mode="after")
    def _infer_version_from_split(self):
        """对齐 harbor CLI 行为：OssRegistryInfo 有 split 时自动填充 version。"""
        if self.version is None and isinstance(self.registry, OssRegistryInfo) and self.registry.split:
            self.version = (
                f"{self.registry.split}@{self.registry.revision}" if self.registry.revision else self.registry.split
            )
        return self


# 兼容别名
DatasetConfig = LocalDatasetConfig | RegistryDatasetConfig


class JobConfig(BaseModel):
    """Job configuration combining Harbor-native fields with Rock extensions.

    Harbor-native fields are serialized to YAML and passed to `harbor jobs start -c`.
    Rock extension fields control sandbox lifecycle.
    """

    # ── Rock extension fields ──
    sandbox_config: SandboxConfig | None = None
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]",
    )
    sandbox_env: dict[str, str] = Field(
        default_factory=dict,
        description="Shell env vars exported before harbor run (OSS keys, API keys, etc.)",
    )
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
    datasets: list[LocalDatasetConfig | RegistryDatasetConfig] = Field(default_factory=list)
    tasks: list[TaskConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)

    # ── Rock extension field names (excluded from Harbor YAML) ──
    _rock_fields: ClassVar[set[str]] = {
        "sandbox_config",
        "setup_commands",
        "file_uploads",
        "sandbox_env",
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
