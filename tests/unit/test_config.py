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

    config_full = {
        "standard_spec": {
            "memory": "8g",
            "cpus": 2,
        },
        "max_allowed_spec": {
            "memory": "32g",
            "cpus": 4,
        },
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


def test_oss_config_defaults():
    from rock.config import OssAccountConfig, OssConfig

    cfg = OssConfig()
    assert cfg.bucket == ""
    assert cfg.region == ""
    assert cfg.transfer_prefix == ""  # default is now empty; deployments must opt-in via YAML
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == ""
    assert cfg.primary.region == ""


def test_oss_config_primary_dict_coerced():
    from rock.config import OssAccountConfig, OssConfig

    cfg = OssConfig(
        primary={
            "endpoint": "e",
            "bucket": "chatos-rock",
            "access_key_id": "a",
            "access_key_secret": "s",
            "role_arn": "r",
            "region": "cn-hangzhou",
        }
    )
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == "chatos-rock"
    assert cfg.primary.region == "cn-hangzhou"
    # legacy 顶层字段未提供时仍为默认空,确认 primary 不会污染 legacy
    assert cfg.bucket == ""

