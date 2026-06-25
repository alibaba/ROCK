"""Tests for _apply_image_os_profile in sandbox_api.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.entrypoints import sandbox_api
from rock.deployments.config import DockerDeploymentConfig


def _make_config(image: str = "python:3.11", image_os: str = "linux", **overrides) -> DockerDeploymentConfig:
    return DockerDeploymentConfig(image=image, image_os=image_os, **overrides)


def _setup_sandbox_manager(
    *,
    yaml_profiles: dict | None = None,
    nacos_config: dict | None = None,
):
    mock_manager = MagicMock()
    mock_manager.rock_config.runtime.image_os_profiles = yaml_profiles or {}

    if nacos_config is not None:
        nacos = MagicMock()
        nacos.get_config = AsyncMock(return_value=nacos_config)
        mock_manager.rock_config.nacos_provider = nacos
    else:
        mock_manager.rock_config.nacos_provider = None

    sandbox_api.sandbox_manager = mock_manager
    return mock_manager


_ANDROID_PROFILE = {
    "runtime_env": {
        "volume_mounts": [],
        "rocklet_start_cmd": "rocklet --port {proxy_port}",
    },
    "startup_timeout": 300,
}


class TestApplyImageOsProfile:
    # --- matching ---

    @pytest.mark.asyncio
    async def test_no_profiles_no_op(self):
        _setup_sandbox_manager()
        config = _make_config()
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is None

    @pytest.mark.asyncio
    async def test_image_os_matches_yaml_profile(self):
        _setup_sandbox_manager(yaml_profiles={"android": _ANDROID_PROFILE})
        config = _make_config(image_os="android")
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is not None
        assert config.image_os_profile["name"] == "android"

    @pytest.mark.asyncio
    async def test_image_os_no_match_profile_stays_none(self):
        _setup_sandbox_manager(yaml_profiles={"android": _ANDROID_PROFILE})
        config = _make_config(image_os="linux")
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is None

    # --- startup_timeout from profile ---

    @pytest.mark.asyncio
    async def test_profile_timeout_applied_when_sdk_did_not_set(self):
        _setup_sandbox_manager(yaml_profiles={"android": _ANDROID_PROFILE})
        config = _make_config(image_os="android")
        await sandbox_api._apply_image_os_profile(config)
        assert config.startup_timeout == 300

    @pytest.mark.asyncio
    async def test_profile_timeout_does_not_override_sdk_value(self):
        _setup_sandbox_manager(yaml_profiles={"android": _ANDROID_PROFILE})
        config = _make_config(image_os="android", startup_timeout=600)
        await sandbox_api._apply_image_os_profile(config)
        assert config.startup_timeout == 600

    # --- YAML + Nacos merge ---

    @pytest.mark.asyncio
    async def test_nacos_adds_new_profile(self):
        _setup_sandbox_manager(
            yaml_profiles={"android": _ANDROID_PROFILE},
            nacos_config={
                "image_os_profiles": {
                    "windows": {"runtime_env": {"rocklet_start_cmd": "rocklet --port {proxy_port}"}},
                }
            },
        )
        config = _make_config(image_os="windows")
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is not None
        assert config.image_os_profile["name"] == "windows"

    @pytest.mark.asyncio
    async def test_nacos_overrides_yaml_same_name_whole_profile(self):
        nacos_android = {"runtime_env": {"rocklet_start_cmd": "custom-cmd"}, "startup_timeout": 120}
        _setup_sandbox_manager(
            yaml_profiles={"android": _ANDROID_PROFILE},
            nacos_config={"image_os_profiles": {"android": nacos_android}},
        )
        config = _make_config(image_os="android")
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is not None
        assert config.image_os_profile["runtime_env"]["rocklet_start_cmd"] == "custom-cmd"
        assert config.startup_timeout == 120

    @pytest.mark.asyncio
    async def test_yaml_profile_still_reachable_after_nacos_adds_different_name(self):
        _setup_sandbox_manager(
            yaml_profiles={"android": _ANDROID_PROFILE},
            nacos_config={"image_os_profiles": {"windows": {"runtime_env": {}}}},
        )
        config = _make_config(image_os="android")
        await sandbox_api._apply_image_os_profile(config)
        assert config.image_os_profile is not None
        assert config.image_os_profile["name"] == "android"

    # --- runtime_env nesting ---

    @pytest.mark.asyncio
    async def test_profile_preserves_runtime_env_in_config(self):
        _setup_sandbox_manager(yaml_profiles={"android": _ANDROID_PROFILE})
        config = _make_config(image_os="android")
        await sandbox_api._apply_image_os_profile(config)
        assert "runtime_env" in config.image_os_profile
        assert config.image_os_profile["runtime_env"]["rocklet_start_cmd"] == "rocklet --port {proxy_port}"
