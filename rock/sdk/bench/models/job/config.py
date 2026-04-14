"""Job configuration models aligned with harbor.models.job.config.

Harbor-native fields are serialized to YAML and passed to ``harbor jobs start -c``.
JobConfig inherits from rock.sdk.job.config.JobConfig (base).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from rock.sdk.bench.constants import USER_DEFINED_LOGS
from rock.sdk.bench.models.metric.config import MetricConfig
from rock.sdk.bench.models.orchestrator_type import OrchestratorType
from rock.sdk.bench.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    OssMirrorConfig,
    RockEnvironmentConfig,  # noqa: F401 — re-exported for backward compat
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.job.config import JobConfig as _BaseJobConfig

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


class HarborJobConfig(_BaseJobConfig):
    """Harbor Job configuration: extends base JobConfig with Harbor-native fields.

    All Rock sandbox/lifecycle configuration lives in ``environment`` (inherited).
    Harbor-native fields (agents, datasets, etc.) are serialized to YAML
    and passed to ``harbor jobs start -c``.
    """

    # ── Harbor native fields (base fields: environment, job_name, namespace, etc. are inherited) ──
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

    @model_validator(mode="after")
    def _sync_auto_stop(self):
        """G7: keep top-level auto_stop and environment.auto_stop in sync (OR semantics).

        Users may set either. Legacy ``environment.auto_stop=True`` (pre-job-refactor)
        must still work; new ``config.auto_stop=True`` must also propagate down to
        the environment so the RockEnvironmentConfig path reads the same value.
        """
        effective = bool(self.auto_stop) or bool(self.environment.auto_stop)
        self.auto_stop = effective
        self.environment.auto_stop = effective
        return self

    @model_validator(mode="after")
    def _auto_job_name(self):
        """G3: auto-generate job_name when user omitted it.

        Format: {dataset_name}_{task_name if single task}_{uuid[:8]}
        Matches legacy bench/job.py::_generate_default_job_name.
        """
        import uuid as _uuid

        if self.job_name is not None:
            return self

        parts: list[str] = []
        if self.datasets:
            ds = self.datasets[0]
            if getattr(ds, "name", None):
                parts.append(ds.name)
            task_names = getattr(ds, "task_names", None) or []
            if len(task_names) == 1:
                parts.append(task_names[0])

        parts.append(_uuid.uuid4().hex[:8])
        self.job_name = "_".join(parts)
        return self

    @model_validator(mode="after")
    def _compute_effective_timeout(self):
        """G2: derive wait timeout from agent config × multiplier + buffer.

        Rule (aligned with legacy bench/job.py::_get_wait_timeout):
          agent_timeout = agents[0].max_timeout_sec or agents[0].override_timeout_sec
          effective = int(agent_timeout * multiplier) + 600  (env + verifier buffer)
          fallback  = int(DEFAULT_WAIT_TIMEOUT * multiplier)    (7200 * multiplier)

        Applied only when the base-class default (3600) has not been overridden by
        the user. If the user explicitly set ``timeout`` to a non-default value,
        that wins — we do not second-guess an explicit knob. NOTE: this heuristic
        misfires if a user explicitly picks 3600, but that's considered extremely
        rare; documented limitation.
        """
        from rock.sdk.bench.constants import DEFAULT_WAIT_TIMEOUT

        # 3600 is the base JobConfig default; treat as "user didn't touch it".
        if self.timeout != 3600:
            return self

        multiplier = self.timeout_multiplier or 1.0
        agent_timeout: float | None = None
        if self.agents:
            a = self.agents[0]
            agent_timeout = a.max_timeout_sec or a.override_timeout_sec

        if agent_timeout:
            self.timeout = int(agent_timeout * multiplier) + 600
        else:
            self.timeout = int(DEFAULT_WAIT_TIMEOUT * multiplier)
        return self

    # Base JobConfig fields to exclude when serializing to Harbor YAML
    _BASE_FIELDS: ClassVar[set[str]] = set(_BaseJobConfig.model_fields.keys())

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML for ``harbor jobs start -c``.

        Base JobConfig fields (environment, setup_commands, etc.) are excluded.
        ``job_name`` is re-injected so harbor uses it as the job directory name
        instead of its default timestamp-based naming.
        Harbor environment fields (force_build, override_cpus, etc.)
        are re-injected under ``environment``.
        """
        import yaml

        data = self.model_dump(mode="json", exclude=self._BASE_FIELDS, exclude_none=True)
        if self.job_name:
            data["job_name"] = self.job_name
        harbor_env = self.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str) -> HarborJobConfig:
        """Load HarborJobConfig from a Harbor YAML config file."""
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
