from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.models.environment_type import EnvironmentType


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
