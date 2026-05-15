"""Tests for rock.deployments.log_cleanup — LogCleanupPolicy enum and sentinel constant."""

from rock.deployments.log_cleanup import LOG_STOPPED_SENTINEL, LogCleanupPolicy


class TestLogCleanupPolicy:
    def test_enum_has_three_values(self):
        assert len(LogCleanupPolicy) == 3

    def test_keep_value(self):
        assert LogCleanupPolicy.KEEP == "keep"
        assert LogCleanupPolicy.KEEP.value == "keep"

    def test_keep_then_archive_value(self):
        assert LogCleanupPolicy.KEEP_THEN_ARCHIVE == "keep_then_archive"
        assert LogCleanupPolicy.KEEP_THEN_ARCHIVE.value == "keep_then_archive"

    def test_clean_directly_value(self):
        assert LogCleanupPolicy.CLEAN_DIRECTLY == "clean_directly"
        assert LogCleanupPolicy.CLEAN_DIRECTLY.value == "clean_directly"

    def test_string_construction(self):
        """Enum is str-based, so string construction must work (used in YAML deserialization)."""
        assert LogCleanupPolicy("keep") is LogCleanupPolicy.KEEP
        assert LogCleanupPolicy("keep_then_archive") is LogCleanupPolicy.KEEP_THEN_ARCHIVE
        assert LogCleanupPolicy("clean_directly") is LogCleanupPolicy.CLEAN_DIRECTLY

    def test_sentinel_filename_constant(self):
        assert LOG_STOPPED_SENTINEL == ".rock_stopped_at"

    def test_policy_is_str_subclass(self):
        """LogCleanupPolicy inherits from str for easy YAML/JSON handling."""
        assert isinstance(LogCleanupPolicy.KEEP, str)
