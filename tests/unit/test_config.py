import pytest

from rock.config import RockConfig, RuntimeConfig


@pytest.mark.asyncio
async def test_rock_config():
    rock_config: RockConfig = RockConfig.from_env()
    assert rock_config

@pytest.mark.asyncio
async def test_runtime_config():
    config = {
        "standard_spec": {
            "memory": "8g",
            "cpus": 2,
        },
    }
    runtime_config = RuntimeConfig(**config)

    assert runtime_config.max_allowed_spec.memory == "64g"
    assert runtime_config.max_allowed_spec.cpus == 16
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2
    assert runtime_config.enable_gpu_passthrough is False
    assert runtime_config.gpu_device_request == "all"
    assert runtime_config.gpu_allocation_mode == "fixed"
    assert runtime_config.gpu_count_per_sandbox == 1


@pytest.mark.asyncio
async def test_runtime_config_gpu_fields():
    runtime_config = RuntimeConfig(
        enable_gpu_passthrough=True,
        gpu_device_request="device=1",
        gpu_allocation_mode="round_robin",
        gpu_count_per_sandbox=2,
    )

    assert runtime_config.enable_gpu_passthrough is True
    assert runtime_config.gpu_device_request == "device=1"
    assert runtime_config.gpu_allocation_mode == "round_robin"
    assert runtime_config.gpu_count_per_sandbox == 2

    config_full = {
        "standard_spec": {
            "memory": "8g",
            "cpus": 2,
        },
        "max_allowed_spec": {
            "memory": "32g",
            "cpus": 4,
        }
    }

    runtime_config = RuntimeConfig(**config_full)

    assert runtime_config.max_allowed_spec.memory == "32g"
    assert runtime_config.max_allowed_spec.cpus == 4
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2

    runtime_config = RuntimeConfig()

    assert runtime_config.max_allowed_spec.memory == "64g"
    assert runtime_config.max_allowed_spec.cpus == 16
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2
