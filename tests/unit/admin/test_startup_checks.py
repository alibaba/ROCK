"""Tests for rock.admin.startup_checks — admin startup consistency checks."""

import logging

import pytest

from rock.admin.startup_checks import check_oss_consistency_with_log_policy
from rock.config import OssAccountConfig, OssConfig, RockConfig, SandboxConfig
from rock.deployments.log_cleanup import LogCleanupPolicy


def _make_rock_config(policy: LogCleanupPolicy, primary_bucket: str = "") -> RockConfig:
    """Helper to create a minimal RockConfig with specified policy and bucket."""
    config = object.__new__(RockConfig)
    config.sandbox_config = SandboxConfig(sandbox_log_cleanup_policy_default=policy)
    config.oss = OssConfig(primary=OssAccountConfig(bucket=primary_bucket))
    return config


@pytest.fixture(autouse=True)
def _enable_logger_propagation():
    """Ensure the startup_checks logger propagates so caplog captures it."""
    logger = logging.getLogger("rock.admin.startup_checks")
    old_propagate = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = old_propagate


class TestCheckOssConsistencyWithLogPolicy:
    def test_warns_when_keep_then_archive_and_bucket_empty(self, caplog):
        """KEEP_THEN_ARCHIVE + empty primary.bucket → emit WARNING."""
        rock_config = _make_rock_config(LogCleanupPolicy.KEEP_THEN_ARCHIVE, primary_bucket="")
        with caplog.at_level(logging.WARNING, logger="rock.admin.startup_checks"):
            check_oss_consistency_with_log_policy(rock_config)
        assert "keep_then_archive" in caplog.text
        assert "primary.bucket is empty" in caplog.text

    def test_no_warn_when_keep_then_archive_and_bucket_configured(self, caplog):
        """KEEP_THEN_ARCHIVE + non-empty primary.bucket → no WARNING."""
        rock_config = _make_rock_config(LogCleanupPolicy.KEEP_THEN_ARCHIVE, primary_bucket="chatos-rock")
        with caplog.at_level(logging.WARNING, logger="rock.admin.startup_checks"):
            check_oss_consistency_with_log_policy(rock_config)
        assert "primary.bucket is empty" not in caplog.text

    def test_no_warn_when_policy_is_keep(self, caplog):
        """KEEP + empty primary.bucket → no WARNING (archive not needed)."""
        rock_config = _make_rock_config(LogCleanupPolicy.KEEP, primary_bucket="")
        with caplog.at_level(logging.WARNING, logger="rock.admin.startup_checks"):
            check_oss_consistency_with_log_policy(rock_config)
        assert "primary.bucket is empty" not in caplog.text

    def test_no_warn_when_policy_is_clean_directly(self, caplog):
        """CLEAN_DIRECTLY + empty primary.bucket → no WARNING."""
        rock_config = _make_rock_config(LogCleanupPolicy.CLEAN_DIRECTLY, primary_bucket="")
        with caplog.at_level(logging.WARNING, logger="rock.admin.startup_checks"):
            check_oss_consistency_with_log_policy(rock_config)
        assert "primary.bucket is empty" not in caplog.text
