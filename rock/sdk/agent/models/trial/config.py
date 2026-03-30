from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.models.environment_type import EnvironmentType
from rock.sdk.sandbox.config import SandboxConfig


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
    env: dict[str, str] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class RockEnvironmentConfig(SandboxConfig, EnvironmentConfig):
    """Unified Rock environment config.

    Combines sandbox lifecycle fields (image, memory, cpus, ...) with
    harbor environment fields (force_build, override_cpus, ...) in a single
    flat block. Rock-specific fields are stripped when serializing to Harbor
    YAML via to_harbor_environment().
    """

    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]",
    )
    auto_stop: bool = False

    def to_harbor_environment(self) -> dict:
        """Return only harbor-native environment fields, discarding Rock-only fields.

        Uses model_validate upcast — unknown fields (Rock-only) are silently ignored.
        """
        harbor = EnvironmentConfig.model_validate(self.model_dump(mode="json"))
        return harbor.model_dump(mode="json", exclude_none=True)


class VerifierConfig(BaseModel):
    override_timeout_sec: float | None = None
    max_timeout_sec: float | None = None
    disable: bool = False


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
