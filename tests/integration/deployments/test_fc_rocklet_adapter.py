"""
FC Rocklet Adapter Production Tests

Tests for the production-ready adapter implementation including
configuration, session state tracking, metrics, and TTL cleanup.

Test Coverage:
- IT-ADAPTER-01: AdapterConfig validation and defaults
- IT-ADAPTER-02: SessionState tracking functionality
- IT-ADAPTER-03: Metrics collection and reporting
- IT-ADAPTER-04: Session management (limits, duplicates)
- IT-ADAPTER-05: TTL cleanup mechanism
"""

import os
import time
from unittest.mock import patch, MagicMock

import pytest


# ============================================================
# IT-ADAPTER-01: AdapterConfig
# ============================================================


class TestAdapterConfig:
    """Integration tests for AdapterConfig.

    Purpose: Verify adapter configuration defaults and environment loading.
    """

    def test_default_values(self):
        """IT-ADAPTER-01a: Verify default adapter configuration values."""
        from rock.deployments.fc_rocklet.adapter.server import AdapterConfig

        config = AdapterConfig()

        assert config.max_sessions == 100
        assert config.session_ttl_seconds == 600
        assert config.cleanup_interval_seconds == 60
        assert config.default_timeout == 60
        assert config.max_timeout == 300
        assert config.max_retries == 3
        assert config.retry_delay == 1.0

    def test_from_env_uses_environment_variables(self):
        """IT-ADAPTER-01b: Verify from_env() reads environment variables."""
        from rock.deployments.fc_rocklet.adapter.server import AdapterConfig

        with patch.dict(os.environ, {
            "FC_MAX_SESSIONS": "50",
            "FC_SESSION_TTL": "300",
            "FC_CLEANUP_INTERVAL": "30",
            "FC_DEFAULT_TIMEOUT": "30",
            "FC_MAX_TIMEOUT": "120",
        }):
            config = AdapterConfig.from_env()

            assert config.max_sessions == 50
            assert config.session_ttl_seconds == 300
            assert config.cleanup_interval_seconds == 30
            assert config.default_timeout == 30
            assert config.max_timeout == 120

    def test_custom_values(self):
        """IT-ADAPTER-01c: Verify custom adapter configuration."""
        from rock.deployments.fc_rocklet.adapter.server import AdapterConfig

        config = AdapterConfig(
            max_sessions=200,
            session_ttl_seconds=1200,
            default_timeout=90,
        )

        assert config.max_sessions == 200
        assert config.session_ttl_seconds == 1200
        assert config.default_timeout == 90


# ============================================================
# IT-ADAPTER-02: SessionState (Adapter)
# ============================================================


class TestAdapterSessionState:
    """Integration tests for adapter SessionState.

    Purpose: Verify session state tracking and TTL functionality.
    """

    def test_default_values(self):
        """IT-ADAPTER-02a: Verify SessionState default values."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test-session")

        assert state.session_id == "test-session"
        assert state.command_count == 0
        assert state.error_count == 0

    def test_touch_updates_last_activity(self):
        """IT-ADAPTER-02b: Verify touch() updates last_activity."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")
        initial_activity = state.last_activity

        time.sleep(0.01)
        state.touch()

        assert state.last_activity > initial_activity

    def test_increment_command(self):
        """IT-ADAPTER-02c: Verify increment_command() increases count and touches."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")
        initial_activity = state.last_activity

        time.sleep(0.01)
        state.increment_command()

        assert state.command_count == 1
        assert state.last_activity > initial_activity

    def test_increment_error(self):
        """IT-ADAPTER-02d: Verify increment_error() increases error count."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")
        state.increment_error()
        state.increment_error()

        assert state.error_count == 2

    def test_age_property(self):
        """IT-ADAPTER-02e: Verify age property returns elapsed time."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")
        time.sleep(0.1)

        assert state.age >= 0.1

    def test_idle_time_property(self):
        """IT-ADAPTER-02f: Verify idle_time property returns time since activity."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")
        time.sleep(0.1)

        assert state.idle_time >= 0.1

    def test_is_expired(self):
        """IT-ADAPTER-02g: Verify is_expired() checks TTL correctly."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="test")

        # Should not be expired immediately
        assert state.is_expired(ttl=600) is False

        # Simulate aging by patching time
        with patch("time.time", return_value=state.created_at + 601):
            assert state.is_expired(ttl=600) is True


# ============================================================
# IT-ADAPTER-03: Metrics
# ============================================================


class TestAdapterMetrics:
    """Integration tests for Metrics collection.

    Purpose: Verify metrics tracking and reporting.
    """

    def test_initial_state(self):
        """IT-ADAPTER-03a: Verify Metrics initial state."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()

        assert metrics.total_requests == 0
        assert metrics.successful_requests == 0
        assert metrics.failed_requests == 0
        assert metrics.current_sessions == 0

    def test_record_request_success(self):
        """IT-ADAPTER-03b: Verify record_request with success."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()
        metrics.record_request(success=True)

        assert metrics.total_requests == 1
        assert metrics.successful_requests == 1
        assert metrics.failed_requests == 0

    def test_record_request_failure(self):
        """IT-ADAPTER-03c: Verify record_request with failure."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()
        metrics.record_request(success=False)

        assert metrics.total_requests == 1
        assert metrics.successful_requests == 0
        assert metrics.failed_requests == 1

    def test_record_session_lifecycle(self):
        """IT-ADAPTER-03d: Verify session lifecycle metrics."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()

        metrics.record_session_created()
        assert metrics.total_sessions_created == 1
        assert metrics.current_sessions == 1

        metrics.record_session_created()
        assert metrics.current_sessions == 2

        metrics.record_session_closed()
        assert metrics.total_sessions_closed == 1
        assert metrics.current_sessions == 1

    def test_record_session_expired(self):
        """IT-ADAPTER-03e: Verify session expiration metrics."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()
        metrics.record_session_created()
        metrics.record_session_expired()

        assert metrics.total_sessions_expired == 1
        assert metrics.current_sessions == 0

    def test_to_dict_format(self):
        """IT-ADAPTER-03f: Verify to_dict() output format."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()
        metrics.record_request(success=True)
        metrics.record_request(success=True)
        metrics.record_request(success=False)
        metrics.record_session_created()

        result = metrics.to_dict()

        assert "uptime_seconds" in result
        assert result["total_requests"] == 3
        assert result["successful_requests"] == 2
        assert result["failed_requests"] == 1
        assert result["success_rate"] == 66.67
        assert result["current_sessions"] == 1

    def test_success_rate_calculation(self):
        """IT-ADAPTER-03g: Verify success_rate calculation with no requests."""
        from rock.deployments.fc_rocklet.adapter.server import Metrics

        metrics = Metrics()
        result = metrics.to_dict()

        assert result["success_rate"] == 0


# ============================================================
# IT-ADAPTER-04: Session Management
# ============================================================


class TestAdapterSessionManagement:
    """Integration tests for adapter session management.

    Purpose: Verify session limits, duplicates, and lifecycle.
    """

    def test_create_session_success(self):
        """IT-ADAPTER-04a: Verify successful session creation."""
        from rock.deployments.fc_rocklet.adapter.server import (
            create_session, _sessions, _metrics, _lock
        )

        # Clear state
        with _lock:
            _sessions.clear()
            _metrics.current_sessions = 0

        result = create_session("test-session-1")

        assert result["success"] is True
        assert result["session_id"] == "test-session-1"

        # Cleanup
        with _lock:
            _sessions.clear()

    def test_create_session_duplicate_rejected(self):
        """IT-ADAPTER-04b: Verify duplicate session ID is rejected."""
        from rock.deployments.fc_rocklet.adapter.server import (
            create_session, _sessions, _lock
        )

        # Clear state
        with _lock:
            _sessions.clear()

        # Create first session
        result1 = create_session("duplicate-session")
        assert result1["success"] is True

        # Try to create duplicate
        result2 = create_session("duplicate-session")
        assert result2["success"] is False
        assert "already exists" in result2["error"]

        # Cleanup
        with _lock:
            _sessions.clear()

    def test_create_session_max_sessions_limit(self):
        """IT-ADAPTER-04c: Verify max sessions limit is enforced."""
        from rock.deployments.fc_rocklet.adapter.server import (
            create_session, _sessions, _config, _lock
        )

        # Clear state
        with _lock:
            _sessions.clear()

        # Temporarily set max_sessions to 2
        original_max = _config.max_sessions
        _config.max_sessions = 2

        try:
            # Create sessions up to limit
            create_session("session-1")
            create_session("session-2")

            # Third should fail
            result = create_session("session-3")
            assert result["success"] is False
            assert "Maximum sessions" in result["error"]
        finally:
            _config.max_sessions = original_max
            with _lock:
                _sessions.clear()

    def test_close_session_success(self):
        """IT-ADAPTER-04d: Verify successful session close."""
        from rock.deployments.fc_rocklet.adapter.server import (
            create_session, close_session, _sessions, _lock
        )

        # Clear state
        with _lock:
            _sessions.clear()

        create_session("close-test-session")
        result = close_session("close-test-session")

        assert result["success"] is True

    def test_close_session_nonexistent(self):
        """IT-ADAPTER-04e: Verify closing nonexistent session fails."""
        from rock.deployments.fc_rocklet.adapter.server import close_session

        result = close_session("nonexistent-session")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_close_session_force_nonexistent(self):
        """IT-ADAPTER-04f: Verify force close nonexistent session succeeds."""
        from rock.deployments.fc_rocklet.adapter.server import close_session

        result = close_session("nonexistent-session", force=True)
        assert result["success"] is True


# ============================================================
# IT-ADAPTER-05: TTL Cleanup
# ============================================================


class TestAdapterTTL:
    """Integration tests for adapter TTL cleanup.

    Purpose: Verify session TTL and automatic cleanup.
    """

    def test_session_is_expired_after_ttl(self):
        """IT-ADAPTER-05a: Verify session is_expired after TTL."""
        from rock.deployments.fc_rocklet.adapter.server import SessionState

        state = SessionState(session_id="ttl-test")

        # Not expired initially
        assert state.is_expired(ttl=600) is False

        # Simulate time passing beyond TTL
        with patch("time.time", return_value=state.created_at + 601):
            assert state.is_expired(ttl=600) is True

    def test_cleanup_removes_expired_sessions(self):
        """IT-ADAPTER-05b: Verify cleanup removes expired sessions."""
        from rock.deployments.fc_rocklet.adapter.server import (
            _sessions, _cleanup_expired_sessions, _config, _lock, SessionState
        )

        # Clear state
        with _lock:
            _sessions.clear()

        # Create a session and mark it as expired
        state = SessionState(session_id="expired-session")
        with _lock:
            _sessions["expired-session"] = state

        # Mock time to make session expired
        with patch("time.time") as mock_time:
            # Set current time way past the session's last_activity
            mock_time.return_value = state.last_activity + _config.session_ttl_seconds + 1

            # Run cleanup
            _cleanup_expired_sessions()

        # Session should be removed
        with _lock:
            assert "expired-session" not in _sessions

    def test_cleanup_keeps_active_sessions(self):
        """IT-ADAPTER-05c: Verify cleanup does not remove active sessions."""
        from rock.deployments.fc_rocklet.adapter.server import (
            _sessions, _cleanup_expired_sessions, _lock, SessionState
        )

        # Clear state
        with _lock:
            _sessions.clear()

        # Create an active session
        state = SessionState(session_id="active-session")
        with _lock:
            _sessions["active-session"] = state

        # Run cleanup without time manipulation (session should be fresh)
        _cleanup_expired_sessions()

        # Session should still exist
        with _lock:
            assert "active-session" in _sessions

        # Cleanup
        with _lock:
            _sessions.clear()


# ============================================================
# IT-ADAPTER-06: Route Request
# ============================================================


class TestAdapterRouteRequest:
    """Integration tests for route_request function.

    Purpose: Verify request routing and response handling.
    """

    def test_metrics_endpoint(self):
        """IT-ADAPTER-06a: Verify /metrics endpoint returns metrics."""
        from rock.deployments.fc_rocklet.adapter.server import route_request

        result = route_request("/metrics", "GET", {})

        assert "uptime_seconds" in result
        assert "total_requests" in result
        assert "current_sessions" in result

    def test_health_check_endpoint(self):
        """IT-ADAPTER-06b: Verify /health endpoint returns status."""
        from rock.deployments.fc_rocklet.adapter.server import route_request

        result = route_request("/health", "GET", {})

        assert "status" in result
        assert "runtime" in result
        assert "sessions" in result

    def test_list_sessions_with_stats(self):
        """IT-ADAPTER-06c: Verify /list_sessions returns session stats."""
        from rock.deployments.fc_rocklet.adapter.server import (
            route_request, create_session, _sessions, _lock
        )

        # Clear state
        with _lock:
            _sessions.clear()

        # Create a session
        create_session("list-test-session")

        result = route_request("/list_sessions", "GET", {})

        assert "sessions" in result
        assert "count" in result
        assert result["count"] >= 1

        # Each session should have stats
        sessions = result["sessions"]
        assert len(sessions) >= 1
        assert "session_id" in sessions[0]
        assert "age" in sessions[0]
        assert "commands" in sessions[0]

        # Cleanup
        with _lock:
            _sessions.clear()