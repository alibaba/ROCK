from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from rock.sdk.bench.models.environment_type import EnvironmentType
from rock.sdk.envhub import EnvironmentConfig as _EnvConfig
from rock.sdk.envhub.config import OssMirrorConfig


class AgentConfig(BaseModel):
    name: str | None = None
    import_path: str | None = None
    model_name: str | None = None
    override_timeout_sec: float | None = None
    override_setup_timeout_sec: float | None = None
    max_timeout_sec: float | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)


class EnvironmentConfig(BaseModel):
    type: EnvironmentType | None = None
    import_path: str | None = None
    force_build: bool = False
    delete: bool = True
    override_cpus: int | None = None
    override_memory_mb: int | None = None
    override_storage_mb: int | None = None
    override_gpus: int | None = None
    suppress_override_warnings: bool = False
    mounts_json: list[dict[str, Any]] | None = None
    oss_mirror: OssMirrorConfig | None = None
    oss_deps: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class RockEnvironmentConfig(_EnvConfig, EnvironmentConfig):
    """Unified Rock environment config.

    Combines job environment fields (uploads, env)
    from JobEnvironmentConfig with harbor environment fields (force_build,
    override_cpus, oss_mirror, etc.) from EnvironmentConfig.
    Rock-specific fields are stripped when serializing to Harbor YAML
    via to_harbor_environment().
    """

    def to_harbor_environment(self) -> dict:
        """Return only harbor-native environment fields, discarding Rock-only fields.

        Uses model_validate upcast — unknown fields (Rock-only) are silently ignored.
        """
        harbor = EnvironmentConfig.model_validate(self.model_dump(mode="json"))
        return harbor.model_dump(mode="json", exclude_none=True)


class TemplateConfig(BaseModel):
    """Agent-Bench template reference used by native verifier."""

    name: str | None = None
    revision: str | None = None


class NativeConfig(BaseModel):
    """Config specific to native verifier mode.
    When image and script are both provided, a ContainerVerifier is used to
    run evaluation in an isolated container. Otherwise the built-in SWE-bench
    run_instance flow is used.
    """

    image: str | None = None
    script: str | None = None
    oss_deps: dict[str, str] = Field(default_factory=dict)
    template: TemplateConfig | None = None


class VerifierConfig(BaseModel):
    override_timeout_sec: float | None = None
    max_timeout_sec: float | None = None
    disable: bool = False
    mode: Literal["harbor", "native"] | None = None
    native_config: NativeConfig = Field(default_factory=NativeConfig)


class TaskConfig(BaseModel):
    path: Path
    git_url: str | None = None
    git_commit_id: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None
    source: str | None = None


class ArtifactConfig(BaseModel):
    source: str
    destination: str | None = None
