"""Tests for shared sandbox start configuration normalization."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.common.constants import (
    CPU_OVERCOMMIT_ALLOWED_KEYS_KEY,
    CPU_OVERCOMMIT_HEADROOM_KEY,
    EXTRA_ACCELERATOR_TYPES_KEY,
    KATA_RUNTIME_SWITCH,
    SUPPORT_KATA_SWITCH,
)
from rock.config import SandboxLifecycleConfig
from rock.deployments import start_config
from rock.deployments.config import DockerDeploymentConfig
from rock.sdk.common.exceptions import BadRequestRockError


def _make_rock_config(
    *,
    default_startup_timeout_seconds: float = 600,
    min_startup_timeout_seconds: float = 600,
    max_startup_timeout_seconds: float = 1800,
):
    lifecycle = MagicMock(spec=SandboxLifecycleConfig)
    lifecycle.default_startup_timeout_seconds = default_startup_timeout_seconds
    lifecycle.min_startup_timeout_seconds = min_startup_timeout_seconds
    lifecycle.max_startup_timeout_seconds = max_startup_timeout_seconds

    rock_config = MagicMock()
    rock_config.lifecycle = lifecycle
    return rock_config


@pytest.mark.asyncio
async def test_apply_start_config_runs_all_steps_in_order(monkeypatch):
    calls = []

    def record_sync(name):
        def apply(*args, **kwargs):
            calls.append(name)

        return apply

    def record_async(name):
        async def apply(*args, **kwargs):
            calls.append(name)

        return apply

    monkeypatch.setattr(start_config, "apply_auto_clear_default", record_sync("auto_clear"))
    monkeypatch.setattr(start_config, "apply_accelerator_type_validation", record_async("accelerator"))
    monkeypatch.setattr(start_config, "apply_kata_runtime_switch", record_async("kata_runtime"))
    monkeypatch.setattr(start_config, "apply_kata_disk_size", record_async("kata_disk"))
    monkeypatch.setattr(start_config, "apply_image_os_profile", record_async("image_os"))
    monkeypatch.setattr(start_config, "apply_timeout_defaults", record_async("timeout"))
    monkeypatch.setattr(start_config, "apply_cpu_overcommit_default", record_async("cpu"))
    monkeypatch.setattr(start_config, "apply_disk_limits", record_async("disk"))
    monkeypatch.setattr(start_config, "apply_image_registry_mirror", record_async("mirror"))

    await start_config.apply_start_config(MagicMock(), DockerDeploymentConfig(), "Bearer token")

    assert calls == [
        "auto_clear",
        "accelerator",
        "kata_runtime",
        "kata_disk",
        "image_os",
        "timeout",
        "cpu",
        "disk",
        "mirror",
    ]


def test_auto_clear_default_keeps_e2b_explicit_timeout():
    rock_config = MagicMock()
    rock_config.lifecycle.auto_transition.auto_clear_seconds = 600
    config = DockerDeploymentConfig(auto_clear_time_minutes=61)

    start_config.apply_auto_clear_default(rock_config, config)

    assert config.auto_clear_time_minutes == 61


@pytest.mark.asyncio
async def test_nacos_policy_guards():
    nacos = MagicMock()
    nacos.get_config = AsyncMock(
        return_value={
            EXTRA_ACCELERATOR_TYPES_KEY: ["CUSTOM"],
            CPU_OVERCOMMIT_ALLOWED_KEYS_KEY: ["Bearer allowed"],
            CPU_OVERCOMMIT_HEADROOM_KEY: 1,
        }
    )
    nacos.get_switch_status = AsyncMock(
        side_effect=lambda key, default: key in {SUPPORT_KATA_SWITCH, KATA_RUNTIME_SWITCH}
    )
    nacos.get_config_value = AsyncMock(return_value="80G")
    rock_config = MagicMock(nacos_provider=nacos)

    allowed = DockerDeploymentConfig(accelerator_type="CUSTOM", cpus=2)
    await start_config.apply_accelerator_type_validation(rock_config, allowed)
    await start_config.apply_kata_runtime_switch(rock_config, allowed)
    await start_config.apply_kata_disk_size(rock_config, allowed)
    await start_config.apply_cpu_overcommit_default(rock_config, allowed, "Bearer allowed")

    assert allowed.use_kata_runtime is True
    assert allowed.kata_disk_size == "80G"
    assert allowed.limit_cpus == 3

    with pytest.raises(BadRequestRockError):
        await start_config.apply_accelerator_type_validation(
            rock_config,
            DockerDeploymentConfig(accelerator_type="UNKNOWN"),
        )

    denied = DockerDeploymentConfig(cpus=2)
    await start_config.apply_cpu_overcommit_default(rock_config, denied, "Bearer denied")
    assert denied.limit_cpus is None


@pytest.mark.parametrize(
    ("requested", "default", "minimum", "maximum", "expected"),
    [
        (None, 900, 600, 1800, 900),
        (180, 600, 600, 1800, 600),
        (1500, 600, 600, 1000, 1000),
    ],
)
@pytest.mark.asyncio
async def test_timeout_defaults(requested, default, minimum, maximum, expected):
    rock_config = _make_rock_config(
        default_startup_timeout_seconds=default,
        min_startup_timeout_seconds=minimum,
        max_startup_timeout_seconds=maximum,
    )
    config = DockerDeploymentConfig(startup_timeout=requested)

    await start_config.apply_timeout_defaults(rock_config, config)

    assert config.startup_timeout == expected
