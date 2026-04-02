"""Tests for SandboxTimeoutHelper — pure calculation, no I/O."""

import time

from rock.sandbox.utils.timeout import SandboxTimeoutHelper


class TestMakeTimeoutInfo:
    def test_contains_correct_keys(self):
        info = SandboxTimeoutHelper.make_timeout_info(30)
        assert "auto_clear_time" in info
        assert "expire_time" in info

    def test_expire_time_is_now_plus_duration(self):
        before = int(time.time())
        info = SandboxTimeoutHelper.make_timeout_info(30)
        after = int(time.time())
        expire = int(info["expire_time"])
        assert before + 30 * 60 <= expire <= after + 30 * 60

    def test_auto_clear_time_stored_as_string(self):
        info = SandboxTimeoutHelper.make_timeout_info(45)
        assert info["auto_clear_time"] == "45"


class TestRefreshTimeout:
    def test_recalculates_expire_time(self):
        old_expire = int(time.time()) - 100
        timeout_info = {"auto_clear_time": "30", "expire_time": str(old_expire)}
        result = SandboxTimeoutHelper.refresh_timeout(timeout_info)
        assert result is not None
        assert int(result["expire_time"]) >= int(time.time()) + 30 * 60 - 5

    def test_preserves_auto_clear_time(self):
        timeout_info = {"auto_clear_time": "60", "expire_time": "0"}
        result = SandboxTimeoutHelper.refresh_timeout(timeout_info)
        assert result["auto_clear_time"] == "60"

    def test_returns_none_when_auto_clear_time_missing(self):
        result = SandboxTimeoutHelper.refresh_timeout({"expire_time": "0"})
        assert result is None


class TestIsExpired:
    def test_returns_true_when_past(self):
        timeout_info = {"auto_clear_time": "30", "expire_time": str(int(time.time()) - 100)}
        assert SandboxTimeoutHelper.is_expired(timeout_info) is True

    def test_returns_false_when_future(self):
        timeout_info = {"auto_clear_time": "30", "expire_time": str(int(time.time()) + 3600)}
        assert SandboxTimeoutHelper.is_expired(timeout_info) is False

    def test_returns_true_when_expire_time_missing(self):
        # Missing key → defaults to 0, always in the past
        assert SandboxTimeoutHelper.is_expired({}) is True
