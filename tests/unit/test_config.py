import pytest

from rock.config import RockConfig, RuntimeConfig


@pytest.mark.asyncio
async def test_rock_config():
    rock_config: RockConfig = RockConfig.from_env()
    assert rock_config

    rock_config: RockConfig = RockConfig.from_env(config_path="./rock-conf/rock-test.yml")
    runtime_config: RuntimeConfig = rock_config.runtime
    assert runtime_config.max_allowed_spec.memory == "64g"
    assert runtime_config.max_allowed_spec.cpus == 16
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2
