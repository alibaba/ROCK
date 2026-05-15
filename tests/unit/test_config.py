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
    assert cfg.transfer_prefix == "rock-transfer/"
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == ""
    assert cfg.primary.region == ""
    # PR-1 archive fields
    assert cfg.archive_prefix == "rock-archives/"
    assert cfg.archive_ttl_days == 30
    assert cfg.keep_days_before_archive == 3
    assert cfg.archive_max_attempts == 3


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


def test_oss_config_archive_fields_overridable():
    from rock.config import OssConfig

    cfg = OssConfig(
        archive_prefix="custom-prefix/",
        archive_ttl_days=7,
        keep_days_before_archive=1,
        archive_max_attempts=5,
    )
    assert cfg.archive_prefix == "custom-prefix/"
    assert cfg.archive_ttl_days == 7
    assert cfg.keep_days_before_archive == 1
    assert cfg.archive_max_attempts == 5


def test_sandbox_config_default_policy():
    from rock.config import SandboxConfig
    from rock.deployments.log_cleanup import LogCleanupPolicy

    cfg = SandboxConfig()
    assert cfg.sandbox_log_cleanup_policy_default == LogCleanupPolicy.KEEP_THEN_ARCHIVE


def test_sandbox_config_policy_string_coercion():
    """YAML deserialization passes string; __post_init__ must coerce to enum."""
    from rock.config import SandboxConfig
    from rock.deployments.log_cleanup import LogCleanupPolicy

    cfg = SandboxConfig(sandbox_log_cleanup_policy_default="keep")
    assert cfg.sandbox_log_cleanup_policy_default == LogCleanupPolicy.KEEP

    cfg2 = SandboxConfig(sandbox_log_cleanup_policy_default="clean_directly")
    assert cfg2.sandbox_log_cleanup_policy_default == LogCleanupPolicy.CLEAN_DIRECTLY
