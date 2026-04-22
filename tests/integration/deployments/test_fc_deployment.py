"""
FC (Function Compute) Integration Tests (IT)

Tests for FC runtime and session management integration.
Validates module interfaces and behaviors match the design specification.

FC (Function Compute) is Alibaba Cloud's serverless compute service.

Note: FC uses direct Runtime management via FCOperator, not the Deployment pattern.

Test Coverage:
- IT-FC-01: FCDeploymentConfig validation and defaults
- IT-FC-03: FCSessionManager WebSocket session management
- IT-FC-04: FCRuntime session operations (create/run/close)
- IT-FC-08: Error handling and recovery
- IT-FC-09: ReconnectConfig and session state
- IT-FC-10: WebSocket reconnection logic
- IT-FC-11: HTTP retry mechanism
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.config import FCDeploymentConfig
from rock.logger import init_logger

logger = init_logger(__name__)


# ============================================================
# IT-FC-01: FCDeploymentConfig validation and defaults
# ============================================================


class TestFCDeploymentConfig:
    """Integration tests for FCDeploymentConfig.

    Purpose: Verify configuration class correctly validates inputs,
    provides defaults, and integrates with deployment factory.
    """

    def test_import_from_config_module(self):
        """IT-FC-00: Verify FCDeploymentConfig can be imported from config module.

        This test ensures the ImportError issue is fixed and won't regress.
        The class must be importable from rock.deployments.config for tests
        and production code to work correctly.
        """
        from rock.deployments.config import FCDeploymentConfig as ImportedConfig

        assert ImportedConfig is not None
        config = ImportedConfig()
        assert config.type == "fc"

    def test_is_deployment_config_subclass(self):
        """IT-FC-00b: Verify FCDeploymentConfig is a DeploymentConfig subclass."""
        from rock.deployments.config import DeploymentConfig, FCDeploymentConfig as ImportedConfig

        assert issubclass(ImportedConfig, DeploymentConfig)

    def test_default_values(self):
        """IT-FC-01a: Verify default configuration values."""
        config = FCDeploymentConfig()

        assert config.type == "fc"
        # All fields are now optional and default to None
        # They will be merged with FCConfig defaults at runtime
        assert config.function_name is None
        assert config.region is None
        assert config.memory is None
        assert config.cpus is None
        assert config.session_id is None
        assert config.session_ttl is None

    def test_custom_values(self):
        """IT-FC-01b: Verify custom configuration values."""
        config = FCDeploymentConfig(
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

    def test_get_deployment_raises_not_implemented(self):
        """IT-FC-01c: Verify get_deployment raises NotImplementedError.

        FC uses direct Runtime management via FCOperator, not the Deployment pattern.
        """
        config = FCDeploymentConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
        )

        with pytest.raises(NotImplementedError, match="FC does not use the Deployment pattern"):
            config.get_deployment()

    def test_session_ttl_custom(self):
        """IT-FC-01d: Verify session_ttl can be set."""
        config = FCDeploymentConfig(session_ttl=7200)  # 2 hours in seconds
        assert config.session_ttl == 7200

    def test_session_ttl_default(self):
        """IT-FC-01e: Verify session_ttl default is None (uses FCConfig)."""
        config = FCDeploymentConfig()
        assert config.session_ttl is None


# ============================================================
# IT-FC-03: FCSessionManager WebSocket session management
# ============================================================


class TestFCSessionManager:
    """Integration tests for FCSessionManager.

    Purpose: Verify WebSocket session creation, command execution,
    and session cleanup.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.deployments.fc import FCSessionManager

        config = FCDeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
            region="cn-hangzhou",
            function_name="test-function",
        )
        return FCSessionManager(config)

    def test_build_websocket_url(self, session_manager):
        """IT-FC-03a: Verify WebSocket URL is built correctly."""
        url = session_manager._websocket_url

        assert "wss://" in url
        assert "test_account" in url
        assert "cn-hangzhou" in url
        assert "stateful-async-invocation" in url

    def test_build_http_url(self, session_manager):
        """IT-FC-03b: Verify HTTP URL is built correctly."""
        url = session_manager._http_url

        assert "https://" in url
        assert "test_account" in url
        assert "invocations" in url

    def test_build_auth_headers(self, session_manager):
        """IT-FC-03c: Verify authentication headers are built correctly."""
        headers = session_manager._build_auth_headers(session_id="test-session")

        assert headers["Content-Type"] == "application/json"
        assert headers["x-fc-account-id"] == "test_account"
        # session_id 使用自定义 Header，与 s.yaml affinityHeaderFieldName 保持一致
        assert headers["x-rock-session-id"] == "test-session"

    @pytest.mark.asyncio
    async def test_is_session_alive_false_for_nonexistent(self, session_manager):
        """IT-FC-03d: Verify is_session_alive returns False for nonexistent session."""
        assert await session_manager.is_session_alive("nonexistent") is False


# ============================================================
# IT-FC-04: FCRuntime session operations
# ============================================================


class TestFCRuntime:
    """Integration tests for FCRuntime.

    Purpose: Verify runtime correctly delegates to SessionManager
    and handles responses.
    """

    @pytest.fixture
    def fc_runtime(self):
        from rock.deployments.fc import FCRuntime

        config = FCDeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FCRuntime(config)

    def test_runtime_initialization(self, fc_runtime):
        """IT-FC-04a: Verify runtime initializes correctly."""
        assert fc_runtime.config is not None
        assert fc_runtime.session_manager is not None
        assert fc_runtime._http_client is None  # Lazy initialization

    @pytest.mark.asyncio
    async def test_runtime_close(self, fc_runtime):
        """IT-FC-04b: Verify runtime close cleans up resources."""
        # Initialize HTTP client
        await fc_runtime._ensure_http_client()
        assert fc_runtime._http_client is not None

        # Close should cleanup
        await fc_runtime.close()
        assert fc_runtime._http_client is None


# ============================================================
# IT-FC-08: Error handling and recovery
# ============================================================


class TestFCErrorHandling:
    """Integration tests for FC error handling.

    Purpose: Verify proper error handling and recovery mechanisms.
    """

    def test_config_missing_credentials(self):
        """IT-FC-08a: Verify config with missing credentials defaults to None."""
        config = FCDeploymentConfig()

        # Missing credentials default to None (can be set via environment or config file)
        assert config.account_id is None
        assert config.access_key_id is None
        assert config.access_key_secret is None

    @pytest.mark.asyncio
    async def test_runtime_initialization_with_config(self):
        """IT-FC-08b: Verify FCRuntime initializes correctly with config."""
        from rock.deployments.fc import FCRuntime

        config = FCDeploymentConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
            function_name="test-function",
            region="cn-hangzhou",
        )
        runtime = FCRuntime(config)

        # Runtime should be initialized but not started
        assert runtime.config is not None
        assert runtime.session_manager is not None
        assert runtime._started is False

        # Cleanup
        await runtime.close()


# ============================================================
# IT-FC-09: ReconnectConfig and SessionState
# ============================================================


class TestReconnectConfig:
    """Integration tests for ReconnectConfig.

    Purpose: Verify reconnection configuration defaults and calculations.
    """

    def test_default_values(self):
        """IT-FC-09a: Verify default reconnection configuration values."""
        from rock.deployments.fc import ReconnectConfig

        config = ReconnectConfig()

        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.backoff_factor == 2.0

    def test_get_delay_exponential_backoff(self):
        """IT-FC-09b: Verify exponential backoff calculation."""
        from rock.deployments.fc import ReconnectConfig

        config = ReconnectConfig(base_delay=1.0, backoff_factor=2.0)

        # Exponential backoff: 1s, 2s, 4s, 8s...
        assert config.get_delay(0) == 1.0
        assert config.get_delay(1) == 2.0
        assert config.get_delay(2) == 4.0
        assert config.get_delay(3) == 8.0

    def test_get_delay_capped_at_max(self):
        """IT-FC-09c: Verify delay is capped at max_delay."""
        from rock.deployments.fc import ReconnectConfig

        config = ReconnectConfig(base_delay=1.0, max_delay=10.0, backoff_factor=2.0)

        # Even with high attempt number, delay should not exceed max_delay
        assert config.get_delay(10) == 10.0
        assert config.get_delay(100) == 10.0

    def test_custom_values(self):
        """IT-FC-09d: Verify custom reconnection configuration."""
        from rock.deployments.fc import ReconnectConfig

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
    """Integration tests for SessionState (fc.py).

    Purpose: Verify session state tracking functionality.
    """

    def test_default_values(self):
        """IT-FC-09e: Verify SessionState default values."""
        from rock.deployments.fc import SessionState

        state = SessionState(session_id="test-session")

        assert state.session_id == "test-session"
        assert state.ps1 == "root@fc:~$ "
        assert state.reconnect_count == 0
        assert state.websocket is None

    def test_touch_updates_last_activity(self):
        """IT-FC-09f: Verify touch() updates last_activity."""
        from rock.deployments.fc import SessionState
        import time

        state = SessionState(session_id="test")
        initial_activity = state.last_activity

        # Wait a bit and touch
        time.sleep(0.01)
        state.touch()

        assert state.last_activity > initial_activity


# ============================================================
# IT-FC-10: WebSocket reconnection logic
# ============================================================


class TestFCSessionManagerReconnect:
    """Integration tests for FCSessionManager reconnection.

    Purpose: Verify WebSocket reconnection behavior.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.deployments.fc import FCSessionManager

        config = FCDeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FCSessionManager(config)

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
        """IT-FC-10a: Verify session creation with retry on first failure."""
        with patch("rock.deployments.fc.websockets") as mock_ws_module:
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
        """IT-FC-10b: Verify _reconnect_session returns True on success."""
        from rock.deployments.fc import SessionState

        # Setup existing session
        state = SessionState(session_id="test-session", ps1="custom$ ")
        session_manager.sessions["test-session"] = state

        with patch("rock.deployments.fc.websockets") as mock_ws_module:
            mock_ws_module.connect = AsyncMock(return_value=mock_websocket)
            mock_ws_module.exceptions.ConnectionClosed = Exception

            result = await session_manager._reconnect_session("test-session")

            assert result is True
            assert state.reconnect_count == 1

    @pytest.mark.asyncio
    async def test_reconnect_session_failure(self, session_manager):
        """IT-FC-10c: Verify _reconnect_session returns False on failure."""
        from rock.deployments.fc import SessionState

        state = SessionState(session_id="test-session")
        session_manager.sessions["test-session"] = state

        with patch("rock.deployments.fc.websockets") as mock_ws_module:
            mock_ws_module.connect = AsyncMock(side_effect=Exception("Connection failed"))
            mock_ws_module.exceptions.ConnectionClosed = Exception

            result = await session_manager._reconnect_session("test-session")

            assert result is False

    @pytest.mark.asyncio
    async def test_get_session_stats(self, session_manager):
        """IT-FC-10d: Verify get_session_stats returns session information."""
        from rock.deployments.fc import SessionState

        state = SessionState(session_id="test-session")
        session_manager.sessions["test-session"] = state

        stats = await session_manager.get_session_stats("test-session")

        assert stats is not None
        assert stats["session_id"] == "test-session"
        assert "reconnect_count" in stats
        assert "is_alive" in stats

    @pytest.mark.asyncio
    async def test_get_session_stats_nonexistent(self, session_manager):
        """IT-FC-10e: Verify get_session_stats returns None for nonexistent session."""
        stats = await session_manager.get_session_stats("nonexistent")
        assert stats is None


# ============================================================
# IT-FC-11: HTTP retry mechanism
# ============================================================


class TestFCRuntimeHttpRetry:
    """Integration tests for FCRuntime HTTP retry mechanism.

    Purpose: Verify HTTP request retry with exponential backoff.
    """

    @pytest.fixture
    def fc_runtime(self):
        from rock.deployments.fc import FCRuntime

        config = FCDeploymentConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
        )
        return FCRuntime(config)

    def test_retryable_status_codes_defined(self, fc_runtime):
        """IT-FC-11a: Verify RETRYABLE_STATUS_CODES contains expected codes."""
        expected_codes = {429, 500, 502, 503, 504}
        assert fc_runtime.RETRYABLE_STATUS_CODES == expected_codes

    @pytest.mark.asyncio
    async def test_retry_on_503_status(self, fc_runtime):
        """IT-FC-11b: Verify retry on 503 status code."""
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

        fc_runtime._http_client = mock_client

        # Use a short delay for testing
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await fc_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=2,
            )

        assert result == {"status": "ok"}
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_400_status(self, fc_runtime):
        """IT-FC-11c: Verify no retry on 400 status code."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.raise_for_status = MagicMock(
            side_effect=Exception("400 Bad Request")
        )

        mock_client.post = AsyncMock(return_value=mock_response)

        fc_runtime._http_client = mock_client

        with pytest.raises(RuntimeError):
            await fc_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=3,
            )

        # Should only call once (no retry for 400)
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_failure_after_max_retries(self, fc_runtime):
        """IT-FC-11d: Verify failure after max retries exhausted."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service Unavailable"

        mock_client.post = AsyncMock(return_value=mock_response)

        fc_runtime._http_client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError) as exc_info:
                await fc_runtime._http_request_with_retry(
                    {"action": "test"},
                    max_retries=2,
                )

        assert "failed after" in str(exc_info.value).lower()
        # Should try max_retries + 1 times
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, fc_runtime):
        """IT-FC-11e: Verify success on first attempt without retry."""
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"result": "success"})
        mock_response.raise_for_status = MagicMock()

        mock_client.post = AsyncMock(return_value=mock_response)

        fc_runtime._http_client = mock_client

        result = await fc_runtime._http_request_with_retry({"action": "test"})

        assert result == {"result": "success"}
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_connection_timeout_triggers_retry(self, fc_runtime):
        """IT-FC-11f: Verify connection timeout triggers retry."""
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

        fc_runtime._http_client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await fc_runtime._http_request_with_retry(
                {"action": "test"},
                max_retries=2,
            )

        assert result == {"status": "ok"}
        assert mock_client.post.call_count == 2