"""Typed configuration for Tinker backend server jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class ServerJobMetadata(BaseModel):
    name: str | None = None
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ServerJobConfig(BaseModel):
    platform: Literal["local"]
    metadata: ServerJobMetadata = Field(default_factory=ServerJobMetadata)


class LocalServerJobConfig(ServerJobConfig):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["local"] = "local"
    repo_path: str = ""
    host: str = "0.0.0.0"
    client_host: str = "127.0.0.1"
    port: int = 9000
    startup_timeout: float = 60
    poll_interval: float = 0.5


ServerJobConfigUnion = LocalServerJobConfig

_server_job_config_adapter = TypeAdapter(ServerJobConfigUnion)


def load_server_job_config(config_path: str | Path) -> ServerJobConfigUnion:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    section = raw.get("tinker_backend")
    if not isinstance(section, dict):
        raise ValueError(f"{path} must define a tinker_backend mapping")
    platform = section.get("platform")
    backend_config = section.get("backend_config") or {}
    if not isinstance(backend_config, dict):
        raise ValueError(f"{path} tinker_backend.backend_config must be a mapping")
    return _server_job_config_adapter.validate_python({"platform": platform, **backend_config})
