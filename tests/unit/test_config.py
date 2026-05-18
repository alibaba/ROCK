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
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == ""
    assert cfg.primary.region == ""
    # transfer_prefix moved to SandboxConfig.file_transfer.prefix; archive_prefix /
    # keep_days_before_archive / archive_max_attempts moved to SandboxConfig.log.
    # OssConfig is now purely OSS connectivity (endpoint / bucket / credentials).
    assert not hasattr(cfg, "transfer_prefix")
    assert not hasattr(cfg, "archive_prefix")
    assert not hasattr(cfg, "keep_days_before_archive")
    assert not hasattr(cfg, "archive_max_attempts")


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


def test_sandbox_log_config_defaults():
    from rock.config import SandboxLogConfig

    cfg = SandboxLogConfig()
    # prefix defaults empty: each deployment YAML must opt-in to a value
    # matching its OSS bucket lifecycle rule (e.g. "rock-archives/").
    assert cfg.archive_prefix == ""
    assert cfg.keep_days_before_archive == 3
    assert cfg.archive_max_attempts == 3


def test_sandbox_log_config_overridable():
    from rock.config import SandboxLogConfig

    cfg = SandboxLogConfig(
        archive_prefix="custom-prefix/",
        keep_days_before_archive=1,
        archive_max_attempts=5,
    )
    assert cfg.archive_prefix == "custom-prefix/"
    assert cfg.keep_days_before_archive == 1
    assert cfg.archive_max_attempts == 5


def test_sandbox_file_transfer_config_defaults():
    from rock.config import SandboxFileTransferConfig

    cfg = SandboxFileTransferConfig()
    # prefix defaults empty; YAML opts in to "rock-transfer/" for the
    # primary bucket's lifecycle-managed transfer area.
    assert cfg.prefix == ""


def test_sandbox_config_nests_log_and_file_transfer():
    # The reorg puts log + file_transfer under SandboxConfig (not OssConfig
    # or root) since they're sandbox-domain concerns.
    from rock.config import SandboxConfig, SandboxFileTransferConfig, SandboxLogConfig

    cfg = SandboxConfig()
    assert isinstance(cfg.log, SandboxLogConfig)
    assert isinstance(cfg.file_transfer, SandboxFileTransferConfig)


def test_sandbox_config_coerces_nested_dicts_from_yaml():
    # yaml.safe_load returns dicts for nested keys; __post_init__ must coerce
    # them into the right dataclass so callers can keep dotted access.
    from rock.config import SandboxConfig, SandboxFileTransferConfig, SandboxLogConfig

    cfg = SandboxConfig(
        log={"archive_prefix": "rock-archives/", "keep_days_before_archive": 7},
        file_transfer={"prefix": "rock-transfer/"},
    )
    assert isinstance(cfg.log, SandboxLogConfig)
    assert cfg.log.archive_prefix == "rock-archives/"
    assert cfg.log.keep_days_before_archive == 7
    assert isinstance(cfg.file_transfer, SandboxFileTransferConfig)
    assert cfg.file_transfer.prefix == "rock-transfer/"
