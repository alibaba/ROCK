"""Tests for rock.deployments.log_cleanup_sentinel — sentinel read/write/bump helpers."""

import json
from datetime import datetime

import pytest

from rock.deployments.log_cleanup_sentinel import (
    LOG_STOPPED_SENTINEL,
    Sentinel,
    SentinelState,
)


@pytest.fixture
def log_dir(tmp_path):
    """Create a temporary directory simulating a sandbox log directory."""
    d = tmp_path / "sandbox-abc123"
    d.mkdir()
    return d


class TestSentinelPath:
    def test_returns_correct_path(self, log_dir):
        result = Sentinel.path(log_dir)
        assert result == log_dir / LOG_STOPPED_SENTINEL


class TestWriteSentinel:
    def test_writes_default_state(self, log_dir):
        Sentinel.write(log_dir)
        target = Sentinel.path(log_dir)
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["version"] == 1
        assert data["attempts"] == 0
        assert "stopped_at" in data
        # stopped_at should be a valid ISO 8601 timestamp
        datetime.fromisoformat(data["stopped_at"])

    def test_writes_custom_state(self, log_dir):
        state = SentinelState(stopped_at="2026-05-10T10:00:00+08:00", attempts=2, version=1)
        Sentinel.write(log_dir, state)
        data = json.loads(Sentinel.path(log_dir).read_text())
        assert data["stopped_at"] == "2026-05-10T10:00:00+08:00"
        assert data["attempts"] == 2
        assert data["version"] == 1

    def test_overwrites_existing_sentinel(self, log_dir):
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-01T00:00:00+08:00", attempts=0))
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-02T00:00:00+08:00", attempts=1))
        data = json.loads(Sentinel.path(log_dir).read_text())
        assert data["stopped_at"] == "2026-05-02T00:00:00+08:00"
        assert data["attempts"] == 1


class TestReadSentinel:
    def test_reads_valid_sentinel(self, log_dir):
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-10T12:00:00+08:00", attempts=1))
        state = Sentinel.read(log_dir)
        assert state is not None
        assert state.stopped_at == "2026-05-10T12:00:00+08:00"
        assert state.attempts == 1
        assert state.version == 1

    def test_returns_none_when_missing(self, log_dir):
        assert Sentinel.read(log_dir) is None

    def test_returns_none_on_corrupt_json(self, log_dir):
        Sentinel.path(log_dir).write_text("not valid json {{{")
        assert Sentinel.read(log_dir) is None

    def test_returns_none_on_missing_stopped_at_key(self, log_dir):
        Sentinel.path(log_dir).write_text(json.dumps({"version": 1, "attempts": 0}))
        assert Sentinel.read(log_dir) is None

    def test_defaults_attempts_to_zero(self, log_dir):
        Sentinel.path(log_dir).write_text(json.dumps({"stopped_at": "2026-05-10T00:00:00Z"}))
        state = Sentinel.read(log_dir)
        assert state is not None
        assert state.attempts == 0


class TestBumpAttempts:
    def test_increments_existing_sentinel(self, log_dir):
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-10T00:00:00+08:00", attempts=0))
        result = Sentinel.bump_attempts(log_dir)
        assert result == 1
        state = Sentinel.read(log_dir)
        assert state.attempts == 1

    def test_increments_multiple_times(self, log_dir):
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-10T00:00:00+08:00", attempts=0))
        Sentinel.bump_attempts(log_dir)
        Sentinel.bump_attempts(log_dir)
        result = Sentinel.bump_attempts(log_dir)
        assert result == 3

    def test_creates_sentinel_if_missing(self, log_dir):
        """If sentinel is missing, bump_attempts recreates with attempts=1."""
        result = Sentinel.bump_attempts(log_dir)
        assert result == 1
        state = Sentinel.read(log_dir)
        assert state is not None
        assert state.attempts == 1


class TestSentinelState:
    def test_now_factory(self):
        state = SentinelState.now()
        assert state.attempts == 0
        assert state.version == 1
        # Verify it's a valid ISO timestamp
        dt = datetime.fromisoformat(state.stopped_at)
        assert dt.tzinfo is not None  # tz-aware

    def test_round_trip(self, log_dir):
        """Write and read back should produce identical state."""
        original = SentinelState(stopped_at="2026-05-13T15:30:00+08:00", attempts=2, version=1)
        Sentinel.write(log_dir, original)
        restored = Sentinel.read(log_dir)
        assert restored.stopped_at == original.stopped_at
        assert restored.attempts == original.attempts
        assert restored.version == original.version


class TestSentinelDump:
    """Sentinel.dump is the single source of truth for the sentinel JSON shape;
    admin-side code that overwrites a remote worker's sentinel via
    runtime.write_file() reuses it to avoid schema drift."""

    def test_returns_json_string_with_all_fields(self):
        s = SentinelState(stopped_at="2026-05-13T10:00:00+08:00", attempts=2, version=1)
        result = json.loads(Sentinel.dump(s))
        assert result == {"stopped_at": "2026-05-13T10:00:00+08:00", "attempts": 2, "version": 1}

    def test_dump_then_load_round_trip(self, log_dir):
        original = SentinelState(stopped_at="2026-05-13T10:00:00+08:00", attempts=3, version=1)
        Sentinel.path(log_dir).write_text(Sentinel.dump(original))
        restored = Sentinel.read(log_dir)
        assert restored.stopped_at == original.stopped_at
        assert restored.attempts == original.attempts


class TestAtomicWrite:
    def test_no_partial_file_left_on_disk_after_write(self, log_dir):
        """After a successful write, only the sentinel exists; no .tmp leftover."""
        Sentinel.write(log_dir, SentinelState(stopped_at="2026-05-13T10:00:00+08:00"))
        leftovers = [p.name for p in log_dir.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
