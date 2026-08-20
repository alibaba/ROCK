"""Tests for shared image OS profile normalization."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.deployments import start_config
from rock.deployments.config import DockerDeploymentConfig

_ANDROID_PROFILE = {
    "runtime_env": {
        "volume_mounts": [],
        "rocklet_start_cmd": "rocklet --port {proxy_port}",
    },
    "startup_timeout": 300,
}


def _make_rock_config(
    *,
    yaml_profiles: dict | None = None,
    nacos_config: dict | None = None,
):
    rock_config = MagicMock()
    rock_config.runtime.image_os_profiles = yaml_profiles or {}
    if nacos_config is None:
        rock_config.nacos_provider = None
    else:
        rock_config.nacos_provider.get_config = AsyncMock(return_value=nacos_config)
    return rock_config


@pytest.mark.asyncio
async def test_yaml_profile_applies_runtime_and_timeout():
    rock_config = _make_rock_config(yaml_profiles={"android": _ANDROID_PROFILE})
    config = DockerDeploymentConfig(image="python:3.11", image_os="android")

    await start_config.apply_image_os_profile(rock_config, config)

    assert config.image_os_profile == {"name": "android", **_ANDROID_PROFILE}
    assert config.startup_timeout == 300


@pytest.mark.asyncio
async def test_profile_does_not_override_explicit_timeout():
    rock_config = _make_rock_config(yaml_profiles={"android": _ANDROID_PROFILE})
    config = DockerDeploymentConfig(image="python:3.11", image_os="android", startup_timeout=600)

    await start_config.apply_image_os_profile(rock_config, config)

    assert config.startup_timeout == 600


@pytest.mark.asyncio
async def test_nacos_profile_overrides_yaml_profile():
    nacos_profile = {"runtime_env": {"rocklet_start_cmd": "custom-cmd"}, "startup_timeout": 120}
    rock_config = _make_rock_config(
        yaml_profiles={"android": _ANDROID_PROFILE},
        nacos_config={"image_os_profiles": {"android": nacos_profile}},
    )
    config = DockerDeploymentConfig(image="python:3.11", image_os="android")

    await start_config.apply_image_os_profile(rock_config, config)

    assert config.image_os_profile == {"name": "android", **nacos_profile}
    assert config.startup_timeout == 120
