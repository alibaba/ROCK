"""
FC3.0 Integration Tests (IT)

Tests for FC3 deployment, runtime, and session management integration.
Validates module interfaces and behaviors match the design specification.

Test Coverage:
- IT-FC3-01: FC3DeploymentConfig validation and defaults
- IT-FC3-02: FC3Deployment lifecycle (start/stop)
- IT-FC3-03: FC3SessionManager WebSocket session management
- IT-FC3-04: FC3Runtime session operations (create/run/close)
- IT-FC3-05: FC function adapter (fc3_rocklet/adapter) - import and runtime tests
- IT-FC3-06: FC function adapter session operations
- IT-FC3-07: SandboxManager FC3 integration
- IT-FC3-08: Error handling and recovery
- IT-FC3-09: ReconnectConfig and session state
- IT-FC3-10: WebSocket reconnection logic
- IT-FC3-11: HTTP retry mechanism
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.config import FC3DeploymentConfig
from rock.logger import init_logger

logger = init_logger(__name__)


# ============================================================
# IT-FC3-01: FC3DeploymentConfig validation and defaults
# ============================================================


class TestFC3DeploymentConfig:
    """Integration tests for FC3DeploymentConfig.

    Purpose: Verify configuration class correctly validates inputs,
    provides defaults, and integrates with deployment factory.
    """

    def test_import_from_config_module(self):
        """IT-FC3-00: Verify FC3DeploymentConfig can be imported from config module.

        This test ensures the ImportError issue is fixed and won't regress.
        The class must be importable from rock.deployments.config for tests
        and production code to work correctly.
        """
        from rock.deployments.config import FC3DeploymentConfig as ImportedConfig

        assert ImportedConfig is not None
        config = ImportedConfig()
        assert config.type == "fc3"

    def test_is_deployment_config_subclass(self):
        """IT-FC3-00b: Verify FC3DeploymentConfig is a DeploymentConfig subclass."""
        from rock.deployments.config import DeploymentConfig, FC3DeploymentConfig as ImportedConfig

        assert issubclass(ImportedConfig, DeploymentConfig)

    def test_default_values(self):
        """IT-FC3-01a: Verify default configuration values."""
        config = FC3DeploymentConfig()

        assert config.type == "fc3"
        assert config.function_name == "rock-rocklet-rt"
        assert config.region == "cn-hangzhou"
        assert config.memory == 4096
        assert config.cpus == 2.0
        assert config.session_affinity_header == "x-rock-session-id"
        assert config.sandbox_ttl_minutes == 10

    def test_custom_values(self):
        """IT-FC3-01b: Verify custom configuration values."""
        config = FC3DeploymentConfig(
            function_name="my-sandbox",
            region="cn-shanghai",
            account_id="12345678",
            access_key_id="ak_test",
            access_key_secret="sk_test",
            memory=16384,
            cpus=4.0,
        )

        assert config.function_name == "my-sandbox"
        assert config.region == "cn-shanghai"
        assert config.account_id == "12345678"
        assert config.memory == 16384
        assert config.cpus == 4.0

    def test_get_deployment_returns_fc3deployment(self):
        """IT-FC3-01c: Verify get_deployment returns FC3Deployment instance."""
        config = FC3DeploymentConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
        )
        deployment = config.get_deployment()

        from rock.deployments.fc3 import FC3Deployment

        assert isinstance(deployment, FC3Deployment)

    def test_auto_clear_time_property(self):
        """IT-FC3-01d: Verify auto_clear_time property."""
        config = FC3DeploymentConfig(sandbox_ttl_minutes=120)
        assert config.auto_clear_time == 120

    def test_sandbox_ttl_default(self):
        """IT-FC3-01e: Verify sandbox_ttl_minutes default."""
        config = FC3DeploymentConfig()
        assert config.auto_clear_time == 10


# ============================================================
# IT-FC3-02: FC3Deployment lifecycle
# ============================================================


class TestFC3Deployment:
    """Integration tests for FC3Deployment lifecycle.

    Purpose: Verify deployment start/stop correctly initializes
    and cleans up FC3Runtime.
    """

    @pytest.fixture
    def fc3_config(self):
        return FC3DeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
            function_name="test-function",
        )

    @pytest.mark.asyncio
    async def test_deployment_start(self, fc3_config):
        """IT-FC3-02a: Verify deployment start initializes runtime."""
        from rock.deployments.fc3 import FC3Deployment, FC3Runtime

        deployment = FC3Deployment.from_config(fc3_config)

        # Mock the runtime's is_alive to avoid actual network calls
        with patch.object(FC3Runtime, "is_alive", return_value=True):
            await deployment.start()

        assert deployment._started is True
        assert deployment._runtime is not None
        assert deployment._sandbox_id is not None
        assert deployment._sandbox_id.startswith("fc3-")

        # Cleanup
        await deployment.stop()

    @pytest.mark.asyncio
    async def test_deployment_stop(self, fc3_config):
        """IT-FC3-02b: Verify deployment stop cleans up runtime."""
        from rock.deployments.fc3 import FC3Deployment

        deployment = FC3Deployment.from_config(fc3_config)
        await deployment.start()

        assert deployment._started is True

        await deployment.stop()

        assert deployment._started is False
        assert deployment._runtime is None

    @pytest.mark.asyncio
    async def test_deployment_double_start(self, fc3_config):
        """IT-FC3-02c: Verify double start is handled gracefully."""
        from rock.deployments.fc3 import FC3Deployment

        deployment = FC3Deployment.from_config(fc3_config)
        await deployment.start()
        sandbox_id = deployment._sandbox_id

        # Second start should not change anything
        await deployment.start()

        assert deployment._sandbox_id == sandbox_id

        await deployment.stop()

    def test_set_sandbox_id(self, fc3_config):
        """IT-FC3-02d: Verify sandbox_id can be set before start."""
        from rock.deployments.fc3 import FC3Deployment

        deployment = FC3Deployment.from_config(fc3_config)
        deployment.set_sandbox_id("fc3-custom-id")

        assert deployment.sandbox_id == "fc3-custom-id"


# ============================================================
# IT-FC3-03: FC3SessionManager WebSocket session management
# ============================================================


class TestFC3SessionManager:
    """Integration tests for FC3SessionManager.

    Purpose: Verify WebSocket session creation, command execution,
    and session cleanup.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.deployments.fc3 import FC3SessionManager

        config = FC3DeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FC3SessionManager(config)

    def test_build_websocket_url(self, session_manager):
        """IT-FC3-03a: Verify WebSocket URL is built correctly."""
        url = session_manager._websocket_url

        assert "wss://" in url
        assert "test_account" in url
        assert "cn-hangzhou" in url
        assert "stateful-async-invocation" in url

    def test_build_http_url(self, session_manager):
        """IT-FC3-03b: Verify HTTP URL is built correctly."""
        url = session_manager._http_url

        assert "https://" in url
        assert "test_account" in url
        assert "invocations" in url

    def test_build_auth_headers(self, session_manager):
        """IT-FC3-03c: Verify authentication headers are built correctly."""
        headers = session_manager._build_auth_headers(session_id="test-session")

        assert headers["Content-Type"] == "application/json"
        assert headers["x-fc-account-id"] == "test_account"
        # session_id 使用自定义 Header，与 s.yaml affinityHeaderFieldName 保持一致
        assert headers["x-rock-session-id"] == "test-session"

    def test_is_session_alive_false_for_nonexistent(self, session_manager):
        """IT-FC3-03d: Verify is_session_alive returns False for nonexistent session."""
        assert session_manager.is_session_alive("nonexistent") is False


# ============================================================
# IT-FC3-04: FC3Runtime session operations
# ============================================================


class TestFC3Runtime:
    """Integration tests for FC3Runtime.

    Purpose: Verify runtime correctly delegates to SessionManager
    and handles responses.
    """

    @pytest.fixture
    def fc3_runtime(self):
        from rock.deployments.fc3 import FC3Runtime

        config = FC3DeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FC3Runtime(config)

    def test_runtime_initialization(self, fc3_runtime):
        """IT-FC3-04a: Verify runtime initializes correctly."""
        assert fc3_runtime.config is not None
        assert fc3_runtime.session_manager is not None
        assert fc3_runtime._http_client is None  # Lazy initialization

    @pytest.mark.asyncio
    async def test_runtime_close(self, fc3_runtime):
        """IT-FC3-04b: Verify runtime close cleans up resources."""
        # Initialize HTTP client
        await fc3_runtime._ensure_http_client()
        assert fc3_runtime._http_client is not None

        # Close should cleanup
        await fc3_runtime.close()
        assert fc3_runtime._http_client is None


# ============================================================
# IT-FC3-05: FC function adapter (fc3_rocklet)
# ============================================================


class TestFCFunctionAdapter:
    """Integration tests for FC function adapter.

    Purpose: Verify adapter correctly uses LocalSandboxRuntime from rocklet.
    """

    def test_adapter_import(self):
        """IT-FC3-05a: Verify adapter module can be imported."""
        from rock.deployments.fc3_rocklet.adapter import server

        assert hasattr(server, "create_session")
        assert hasattr(server, "run_in_session")
        assert hasattr(server, "close_session")
        assert hasattr(server, "execute")
        assert hasattr(server, "read_file")
        assert hasattr(server, "write_file")

    def test_adapter_handler_function(self):
        """IT-FC3-05b: Verify WSGI handler exists."""
        from rock.deployments.fc3_rocklet.adapter.server import handler

        assert callable(handler)

    def test_adapter_health_check(self):
        """IT-FC3-05c: Verify health_check function works."""
        try:
            import gem  # noqa: F401
        except ImportError:
            pytest.skip("gem module not available")

        from rock.deployments.fc3_rocklet.adapter.server import health_check

        result = health_check()
        assert result["status"] == "ok"
        assert "sessions" in result

    def test_adapter_local_sandbox_runtime_available(self):
        """IT-FC3-05d: Verify LocalSandboxRuntime is available."""
        try:
            from rock.rocklet.local_sandbox import LocalSandboxRuntime
            assert LocalSandboxRuntime is not None
        except ImportError:
            pytest.skip("LocalSandboxRuntime not available")

    def test_adapter_runtime_initialization(self):
        """IT-FC3-05e: Verify runtime lazy initialization works."""
        try:
            import gem  # noqa: F401
        except ImportError:
            pytest.skip("gem module not available")

        from rock.deployments.fc3_rocklet.adapter.server import _get_runtime

        # Reset runtime
        import rock.deployments.fc3_rocklet.adapter.server as server_module
        server_module._runtime = None

        runtime = _get_runtime()
        assert runtime is not None


# ============================================================
# IT-FC3-06: FC function adapter session operations
# ============================================================


class TestFCFunctionAdapterSession:
    """Integration tests for FC function adapter session operations.

    Purpose: Verify adapter correctly handles session lifecycle.
    """

    def test_route_request_unknown_path(self):
        """IT-FC3-06a: Verify unknown path returns error."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/unknown", "GET", {})
        assert result["success"] is False
        assert "Unknown path" in result["error"]

    def test_route_request_missing_session_id(self):
        """IT-FC3-06b: Verify missing session_id returns error."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/create_session", "POST", {})
        assert result["success"] is False
        assert "session_id" in result["error"]

    def test_route_request_missing_command(self):
        """IT-FC3-06c: Verify missing command returns error."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/run_in_session", "POST", {"session_id": "test"})
        assert result["success"] is False
        assert "command" in result["error"]

    def test_route_request_missing_path_for_file_ops(self):
        """IT-FC3-06d: Verify missing path returns error for file operations."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/read_file", "POST", {})
        assert result["success"] is False
        assert "path" in result["error"]

    def test_list_sessions(self):
        """IT-FC3-06e: Verify list_sessions returns sessions info."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/list_sessions", "GET", {})
        assert "sessions" in result
        assert "count" in result


# ============================================================
# IT-FC3-06-2: FC function adapter file operations
# ============================================================


class TestFCFunctionAdapterFileOps:
    """Integration tests for FC function adapter file operations."""

    def test_route_request_missing_path_write(self):
        """IT-FC3-06-2a: Verify missing path returns error for write_file."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/write_file", "POST", {"content": "test"})
        assert result["success"] is False

    def test_execute_missing_command(self):
        """IT-FC3-06-2b: Verify missing command returns error for execute."""
        from rock.deployments.fc3_rocklet.adapter.server import route_request

        result = route_request("/execute", "POST", {})
        assert result["success"] is False


# ============================================================
# IT-FC3-07: SandboxManager FC3 integration
# ============================================================


class TestSandboxManagerFC3Integration:
    """Integration tests for SandboxManager with FC3.

    Purpose: Verify SandboxManager correctly routes FC3 deployments.
    """

    @pytest.mark.asyncio
    async def test_is_fc3_sandbox_by_id(self):
        """IT-FC3-07a: Verify FC3 sandbox detection checks deployment dict."""
        from rock.sandbox.sandbox_manager import SandboxManager
        from rock.config import RockConfig

        config = RockConfig()
        manager = SandboxManager(config, redis_provider=None)

        # Initially no FC3 sandboxes
        assert manager._is_fc3_sandbox("fc3-abc123") is False

        # Add a mock FC3 deployment
        from unittest.mock import MagicMock
        mock_deployment = MagicMock()
        manager._fc3_deployments["fc3-test-id"] = mock_deployment

        # Now should detect as FC3 sandbox
        assert manager._is_fc3_sandbox("fc3-test-id") is True
        assert manager._is_fc3_sandbox("other-id") is False

    @pytest.mark.asyncio
    async def test_fc3_deployments_dict_initialized(self):
        """IT-FC3-07b: Verify FC3 deployments dict is initialized."""
        from rock.sandbox.sandbox_manager import SandboxManager
        from rock.config import RockConfig

        config = RockConfig()
        manager = SandboxManager(config, redis_provider=None)

        assert hasattr(manager, "_fc3_deployments")
        assert isinstance(manager._fc3_deployments, dict)


# ============================================================
# IT-FC3-08: Error handling and recovery
# ============================================================


class TestFC3ErrorHandling:
    """Integration tests for FC3 error handling.

    Purpose: Verify proper error handling and recovery mechanisms.
    """

    def test_config_missing_credentials(self):
        """IT-FC3-08a: Verify config with missing credentials defaults to None."""
        config = FC3DeploymentConfig()

        # Missing credentials default to None (can be set via environment or config file)
        assert config.account_id is None
        assert config.access_key_id is None
        assert config.access_key_secret is None

    @pytest.mark.asyncio
    async def test_runtime_not_started_error(self):
        """IT-FC3-08b: Verify error when accessing runtime before start."""
        from rock.deployments.fc3 import FC3Deployment

        config = FC3DeploymentConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
        )
        deployment = FC3Deployment.from_config(config)

        # Accessing runtime before start should raise error
        with pytest.raises(Exception):
            _ = deployment.runtime


# ============================================================
# IT-FC3-09: ReconnectConfig and SessionState
# ============================================================


class TestReconnectConfig:
    """Integration tests for ReconnectConfig.

    Purpose: Verify reconnection configuration defaults and calculations.
    """

    def test_default_values(self):
        """IT-FC3-09a: Verify default reconnection configuration values."""
        from rock.deployments.fc3 import ReconnectConfig

        config = ReconnectConfig()

        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.backoff_factor == 2.0

    def test_get_delay_exponential_backoff(self):
        """IT-FC3-09b: Verify exponential backoff calculation."""
        from rock.deployments.fc3 import ReconnectConfig

        config = ReconnectConfig(base_delay=1.0, backoff_factor=2.0)

        # Exponential backoff: 1s, 2s, 4s, 8s...
        assert config.get_delay(0) == 1.0
        assert config.get_delay(1) == 2.0
        assert config.get_delay(2) == 4.0
        assert config.get_delay(3) == 8.0

    def test_get_delay_capped_at_max(self):
        """IT-FC3-09c: Verify delay is capped at max_delay."""
        from rock.deployments.fc3 import ReconnectConfig

        config = ReconnectConfig(base_delay=1.0, max_delay=10.0, backoff_factor=2.0)

        # Even with high attempt number, delay should not exceed max_delay
        assert config.get_delay(10) == 10.0
        assert config.get_delay(100) == 10.0

    def test_custom_values(self):
        """IT-FC3-09d: Verify custom reconnection configuration."""
        from rock.deployments.fc3 import ReconnectConfig

        config = ReconnectConfig(
            max_retries=5,
            base_delay=0.5,
            max_delay=60.0,
            backoff_factor=3.0,
        )

        assert config.max_retries == 5
        assert config.base_delay == 0.5
        assert config.max_delay == 60.0
        assert config.backoff_factor == 3.0


class TestSessionState:
    """Integration tests for SessionState (fc3.py).

    Purpose: Verify session state tracking functionality.
    """

    def test_default_values(self):
        """IT-FC3-09e: Verify SessionState default values."""
        from rock.deployments.fc3 import SessionState

        state = SessionState(session_id="test-session")

        assert state.session_id == "test-session"
        assert state.ps1 == "root@fc:~$ "
        assert state.reconnect_count == 0
        assert state.websocket is None

    def test_touch_updates_last_activity(self):
        """IT-FC3-09f: Verify touch() updates last_activity."""
        from rock.deployments.fc3 import SessionState
        import time

        state = SessionState(session_id="test")
        initial_activity = state.last_activity

        # Wait a bit and touch
        time.sleep(0.01)
        state.touch()

        assert state.last_activity > initial_activity


# ============================================================
# IT-FC3-10: WebSocket reconnection logic
# ============================================================


class TestFC3SessionManagerReconnect:
    """Integration tests for FC3SessionManager reconnection.

    Purpose: Verify WebSocket reconnection behavior.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.deployments.fc3 import FC3SessionManager

        config = FC3DeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FC3SessionManager(config)

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket connection."""
        ws = AsyncMock()
        ws.open = True
        ws.send = AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({"type": "ready"}))
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_create_session_with_retries(self, session_manager, mock_websocket):
        """IT-FC3-10a: Verify session creation with retry on first failure."""
        with patch("rock.deployments.fc3.websockets") as mock_ws_module:
            # First attempt fails, second succeeds
            mock_ws_module.connect = AsyncMock()
            mock_ws_module.connect.side_effect = [
                Exception("Connection failed"),
                mock_websocket,
            ]
            mock_ws_module.exceptions.ConnectionClosed = Exception

            # Should succeed after retry
            result = await session_manager.create_session("test-session")

            assert result is not None
            assert "test-session" in session_manager.sessions

    @pytest.mark.asyncio
    async def test_reconnect_session_success(self, session_manager, mock_websocket):
        """IT-FC3-10b: Verify _reconnect_session returns True on success."""
        from rock.deployments.fc3 import SessionState

        # Setup existing session
        state = SessionState(session_id="test-session", ps1="custom$ ")
        session_manager.sessions["test-session"] = state

        with patch("rock.deployments.fc3.websockets") as mock_ws_module:
            mock_ws_module.connect = AsyncMock(return_value=mock_websocket)
            mock_ws_module.exceptions.ConnectionClosed = Exception

            result = await session_manager._reconnect_session("test-session")

            assert result is True
            assert state.reconnect_count == 1

    @pytest.mark.asyncio
    async def test_reconnect_session_failure(self, session_manager):
        """IT-FC3-10c: Verify _reconnect_session returns False on failure."""
        from rock.deployments.fc3 import SessionState

        state = SessionState(session_id="test-session")
        session_manager.sessions["test-session"] = state

        with patch("rock.deployments.fc3.websockets") as mock_ws_module:
            mock_ws_module.connect = AsyncMock(side_effect=Exception("Connection failed"))
            mock_ws_module.exceptions.ConnectionClosed = Exception

            result = await session_manager._reconnect_session("test-session")

            assert result is False

    def test_get_session_stats(self, session_manager):
        """IT-FC3-10d: Verify get_session_stats returns session information."""
        from rock.deployments.fc3 import SessionState

        state = SessionState(session_id="test-session")
        session_manager.sessions["test-session"] = state

        stats = session_manager.get_session_stats("test-session")

        assert stats is not None
        assert stats["session_id"] == "test-session"
        assert "reconnect_count" in stats
        assert "is_alive" in stats

    def test_get_session_stats_nonexistent(self, session_manager):
        """IT-FC3-10e: Verify get_session_stats returns None for nonexistent session."""
        stats = session_manager.get_session_stats("nonexistent")
        assert stats is None


# ============================================================
# IT-FC3-11: HTTP retry mechanism
# ============================================================


class TestFC3RuntimeHttpRetry:
    """Integration tests for FC3Runtime HTTP retry mechanism.

    Purpose: Verify HTTP request retry with exponential backoff.
    """

    @pytest.fixture
    def fc3_runtime(self):
        from rock.deployments.fc3 import FC3Runtime

        config = FC3DeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FC3Runtime(config)

    def test_retryable_status_codes_defined(self, fc3_runtime):
        """IT-FC3-11a: Verify RETRYABLE_STATUS_CODES contains expected codes."""
        expected_codes = {429, 500, 502, 503, 504}
        assert fc3_runtime.RETRYABLE_STATUS_CODES == expected_codes

    @pytest.mark.asyncio
    async def test_retry_on_503_status(self, fc3_runtime):
        """IT-FC3-11b: Verify retry on 503 status code."""
        mock_client = AsyncMock()

        # First response: 503, second response: 200
        mock_response_503 = MagicMock()
        mock_response_503.status_code = 503
        mock_response_503.text = "Service Unavailable"

        mock_response_200 = MagicMock()
        mock_response_200.status_code = 200
        mock_response_200.json = MagicMock(return_value={"status": "ok"})
        mock_response_200.raise_for_status = MagicMock()

        mock_client.post = AsyncMock(side_effect=[mock_response_503, mock_response_200])

        fc3_runtime._http_client = mock_client

        # Use a short delay for testing
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await fc3_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=2,
            )

        assert result == {"status": "ok"}
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_400_status(self, fc3_runtime):
        """IT-FC3-11c: Verify no retry on 400 status code."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.raise_for_status = MagicMock(
            side_effect=Exception("400 Bad Request")
        )

        mock_client.post = AsyncMock(return_value=mock_response)

        fc3_runtime._http_client = mock_client

        with pytest.raises(RuntimeError):
            await fc3_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=3,
            )

        # Should only call once (no retry for 400)
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_failure_after_max_retries(self, fc3_runtime):
        """IT-FC3-11d: Verify failure after max retries exhausted."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service Unavailable"

        mock_client.post = AsyncMock(return_value=mock_response)

        fc3_runtime._http_client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError) as exc_info:
                await fc3_runtime._http_request_with_retry(
                    {"action": "test"},
                    max_retries=2,
                )

        assert "failed after" in str(exc_info.value).lower()
        # Should try max_retries + 1 times
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, fc3_runtime):
        """IT-FC3-11e: Verify success on first attempt without retry."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"result": "success"})
        mock_response.raise_for_status = MagicMock()

        mock_client.post = AsyncMock(return_value=mock_response)

        fc3_runtime._http_client = mock_client

        result = await fc3_runtime._http_request_with_retry({"action": "test"})

        assert result == {"result": "success"}
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_connection_timeout_triggers_retry(self, fc3_runtime):
        """IT-FC3-11f: Verify connection timeout triggers retry."""
        import asyncio

        mock_client = AsyncMock()

        # First call: timeout, second call: success
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"status": "ok"})
        mock_response.raise_for_status = MagicMock()

        mock_client.post = AsyncMock(
            side_effect=[
                asyncio.TimeoutError("Connection timeout"),
                mock_response,
            ]
        )

        fc3_runtime._http_client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await fc3_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=2,
            )

        assert result == {"status": "ok"}
        assert mock_client.post.call_count == 2