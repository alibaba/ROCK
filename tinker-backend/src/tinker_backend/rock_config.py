"""ROCK-native settings and job templates."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from rock.sdk.job.config import JobConfig


class ROCKJobDefaults(BaseModel):
    """Defaults for model settings and optional ROCK storage configuration."""

    model_config = ConfigDict(extra="allow")
    rock_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None
    oss_bucket: str | None = None
    oss_dataset_path: str | None = None
    oss_endpoint: str | None = None

    @classmethod
    def from_env(cls, defaults: ROCKJobDefaults | None = None) -> ROCKJobDefaults:
        values = defaults.model_dump() if defaults is not None else {}
        for field in set(cls.model_fields) | set(values):
            if field.upper() in os.environ:
                values[field] = os.environ[field.upper()]
        return cls.model_validate(values)


def _get(data: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdecimal() and int(key) < len(data):
            data = data[int(key)]
        else:
            return None
    return data


def _set(data: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    if any(not key for key in keys):
        raise ValueError("Override path has an empty component")
    current = data
    for index, key in enumerate(keys):
        if isinstance(current, list):
            if not key.isdecimal() or int(key) >= len(current):
                raise ValueError("Override array index does not exist")
            key = int(key)
        elif not isinstance(current, dict):
            raise ValueError("Override path traverses a scalar")
        if index == len(keys) - 1:
            current[key] = value
        else:
            if isinstance(current, dict) and current.get(key) is None:
                current[key] = [] if keys[index + 1].isdecimal() else {}
            current = current[key]


def _expand(data: Any, variables: dict[str, str]) -> Any:
    if isinstance(data, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: variables.get(m[1], m[0]), data)
    if isinstance(data, list):
        return [_expand(item, variables) for item in data]
    if isinstance(data, dict):
        return {key: _expand(value, variables) for key, value in data.items()}
    return data


def load_job_config(
    bench_name: str | None,
    *,
    default_config: ROCKJobDefaults | None = None,
    task_names: list[str] | None = None,
    model_name: str | None = None,
    env: dict[str, str] | None = None,
    template_root: str | Path | None = None,
    job_config_path: str | Path | None = None,
    **dotted_overrides: Any,
) -> JobConfig:
    """Apply fallback defaults, explicit arguments, then dotted overrides."""
    if bench_name is not None and (
        not bench_name or bench_name in {".", ".."} or "/" in bench_name or "\\" in bench_name
    ):
        raise ValueError("bench_name must be a single path segment")
    if job_config_path is not None:
        path = Path(job_config_path).expanduser().resolve()
    else:
        if not bench_name:
            raise ValueError("bench_name or job_config_path is required")
        root = (
            Path(template_root).expanduser().resolve()
            if template_root is not None
            else Path(__file__).with_name("templates").resolve()
        )
        path = (root / bench_name / "job_config.yaml").resolve()
        if not path.is_relative_to(root):
            raise ValueError("Benchmark template must remain inside template_root")
    if not path.is_file():
        raise FileNotFoundError("ROCK job template is missing; provide rock.job_config_path or rock.template_root")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ROCK job template must be a YAML mapping")
    defaults = default_config or ROCKJobDefaults.from_env()
    targets = {
        "rock_key": ["environment.xrl_authorization"],
        "openai_api_key": ["environment.env.OPENAI_API_KEY"],
        "openai_base_url": ["environment.env.OPENAI_BASE_URL"],
        "openai_model": ["environment.env.OPENAI_MODEL"],
    }
    for field in (
        "oss_access_key_id",
        "oss_access_key_secret",
        "oss_region",
        "oss_bucket",
        "oss_dataset_path",
        "oss_endpoint",
    ):
        targets[field] = ["environment.env." + field.upper()]
        if data.get("datasets") and _get(data, "datasets.0.registry") is not None:
            targets[field].append("datasets.0.registry." + field)
        if field != "oss_dataset_path" and _get(data, "environment.oss_mirror") is not None:
            targets[field].append("environment.oss_mirror." + field)
    for field, paths in targets.items():
        value = getattr(defaults, field)
        if value is not None:
            for target in paths:
                if _get(data, target) in (None, ""):
                    _set(data, target, value)
    variables = {key.upper(): str(value) for key, value in defaults.model_dump().items() if value is not None}
    variables.update(os.environ)
    data = _expand(data, variables)
    if task_names is not None and data.get("datasets"):
        _set(data, "datasets.0.task_names", task_names)
    if model_name is not None and data.get("agents"):
        _set(data, "agents.0.model_name", model_name)
    if env is not None:
        for key, value in env.items():
            _set(data, "environment.env." + key, value)
        if data.get("agents"):
            if "OPENAI_API_KEY" in env:
                _set(data, "agents.0.kwargs.api_key", env["OPENAI_API_KEY"])
            if "OPENAI_BASE_URL" in env:
                _set(data, "agents.0.kwargs.api_base", env["OPENAI_BASE_URL"])
    for key, value in dotted_overrides.items():
        _set(data, key, value)
    environment = data.get("environment") or {}
    if environment.get("uploads"):
        environment["uploads"] = [
            [str((path.parent / src).resolve()) if not Path(src).is_absolute() else src, dst]
            for src, dst in environment["uploads"]
        ]
    if data.get("script_path") and not Path(data["script_path"]).is_absolute():
        data["script_path"] = str((path.parent / data["script_path"]).resolve())
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle)
        handle.flush()
        return JobConfig.from_yaml(handle.name)
