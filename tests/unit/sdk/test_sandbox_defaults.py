"""Environment defaults are resolved when a sandbox config is constructed."""

import pytest

from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig


@pytest.mark.parametrize(
    ("config_cls", "field", "variable", "value", "expected", "fallback"),
    [
        (SandboxConfig, "image", "ROCK_DEFAULT_IMAGE", "ubuntu:24.04", "ubuntu:24.04", "python:3.11"),
        (SandboxConfig, "memory", "ROCK_DEFAULT_MEMORY", "16g", "16g", "8g"),
        (SandboxConfig, "cpus", "ROCK_DEFAULT_CPUS", "0.5", 0.5, 2),
        (SandboxConfig, "auto_clear_seconds", "ROCK_DEFAULT_AUTO_CLEAR_SECONDS", "900", 900, 300),
        (SandboxGroupConfig, "size", "ROCK_DEFAULT_GROUP_SIZE", "4", 4, 2),
        (SandboxGroupConfig, "start_concurrency", "ROCK_DEFAULT_GROUP_START_CONCURRENCY", "3", 3, 2),
        (SandboxGroupConfig, "start_retry_times", "ROCK_DEFAULT_GROUP_START_RETRY_TIMES", "5", 5, 3),
    ],
)
def test_resource_defaults_and_precedence(monkeypatch, config_cls, field, variable, value, expected, fallback):
    monkeypatch.delenv(variable, raising=False)
    assert getattr(config_cls(), field) == fallback

    monkeypatch.setenv(variable, value)
    assert getattr(config_cls(), field) == expected
    assert getattr(config_cls(**{field: fallback}), field) == fallback

    monkeypatch.delenv(variable)
    assert getattr(config_cls(), field) == fallback
