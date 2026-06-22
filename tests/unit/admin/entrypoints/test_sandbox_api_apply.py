"""Tests for _apply_* helper functions in sandbox_api.py."""

from unittest.mock import MagicMock

import pytest

from rock.admin.entrypoints import sandbox_api
from rock.config import SandboxLifecycleConfig
from rock.deployments.config import DockerDeploymentConfig


def _make_config(**overrides) -> DockerDeploymentConfig:
    return DockerDeploymentConfig(**overrides)


def _setup_sandbox_manager(
    *,
    default_startup_timeout_seconds: float = 600,
    min_startup_timeout_seconds: float = 600,
    max_startup_timeout_seconds: float = 1800,
):
    lifecycle = MagicMock(spec=SandboxLifecycleConfig)
    lifecycle.default_startup_timeout_seconds = default_startup_timeout_seconds
    lifecycle.min_startup_timeout_seconds = min_startup_timeout_seconds
    lifecycle.max_startup_timeout_seconds = max_startup_timeout_seconds

    mock_manager = MagicMock()
    mock_manager.rock_config.lifecycle = lifecycle
    sandbox_api.sandbox_manager = mock_manager
    return mock_manager


# --- _apply_timeout_defaults ---


class TestApplyTimeoutDefaults:
    @pytest.mark.asyncio
    async def test_user_not_set_uses_lifecycle_default(self):
        """SDK didn't pass startup_timeout — lifecycle default (600s) is applied."""
        _setup_sandbox_manager()
        config = _make_config()
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 600

    @pytest.mark.asyncio
    async def test_user_not_set_yaml_default_applied(self):
        _setup_sandbox_manager(default_startup_timeout_seconds=900)
        config = _make_config()
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 900

    @pytest.mark.asyncio
    async def test_user_set_value_kept(self):
        """SDK-set value should not be replaced by default."""
        _setup_sandbox_manager(default_startup_timeout_seconds=900)
        config = _make_config(startup_timeout=700)
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 700

    @pytest.mark.asyncio
    async def test_min_raises_low_sdk_value(self):
        """SDK passes 180 but min is 600 — result clamped up to 600."""
        _setup_sandbox_manager()
        config = _make_config(startup_timeout=180)
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 600

    @pytest.mark.asyncio
    async def test_max_caps_sdk_value(self):
        _setup_sandbox_manager(max_startup_timeout_seconds=1000)
        config = _make_config(startup_timeout=1500)
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 1000

    @pytest.mark.asyncio
    async def test_max_caps_default_value(self):
        _setup_sandbox_manager(default_startup_timeout_seconds=1500, max_startup_timeout_seconds=1000)
        config = _make_config()
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 1000

    @pytest.mark.asyncio
    async def test_sdk_value_within_range_not_changed(self):
        _setup_sandbox_manager()
        config = _make_config(startup_timeout=800)
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 800

    @pytest.mark.asyncio
    async def test_default_min_max_clamp_range(self):
        """lifecycle default=2000 exceeds max=1800, so result is capped at 1800."""
        _setup_sandbox_manager(default_startup_timeout_seconds=2000)
        config = _make_config()
        await sandbox_api._apply_timeout_defaults(config)
        assert config.startup_timeout == 1800
