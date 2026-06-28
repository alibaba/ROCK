"""
FC (Function Compute) Integration Tests (IT)

Tests for FC runtime and session management integration.
Validates module interfaces and behaviors match the SDK InvokeFunction design.

FC (Function Compute) is Alibaba Cloud's serverless compute service.

Note: FC uses Operator-level configuration (FCOperatorConfig), not Deployment-level.

Test Coverage:
- IT-FC-01: FCOperatorConfig validation and defaults
- IT-FC-03: FCSessionManager SDK InvokeFunction session management
- IT-FC-04: FCRuntime session operations (create/run/close)
- IT-FC-08: Error handling and recovery
- IT-FC-09: SessionState and CircuitBreaker
- IT-FC-10: SDK retry and circuit breaker logic
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.logger import init_logger
from rock.sandbox.operator.fc import FCOperatorConfig

logger = init_logger(__name__)


# ============================================================
# Test helpers: fake FC SDK
# ============================================================


def _make_fc_client(response_body: dict | None = None):
    """Create a mock FC SDK client with configurable response.

    By default, get_function_with_options raises to simulate function not existing.
    Tests can set client.get_function_with_options.return_value to simulate existing function.
    """
    client = MagicMock()
    response = MagicMock()
    response.body = json.dumps(response_body or {"output": "root@fc:~$ "})
    client.invoke_function_with_options = MagicMock(return_value=response)
    # By default, function does not exist (get_function raises)
    client.get_function_with_options = MagicMock(side_effect=Exception("FunctionNotFound"))
    # Create/delete function return mock responses
    create_response = MagicMock()
    create_response.body = MagicMock(function_name="mock-function")
    client.create_function_with_options = MagicMock(return_value=create_response)
    client.delete_function_with_options = MagicMock(return_value=MagicMock())
    return client


@pytest.fixture(autouse=True)
def _fake_fc_sdk(monkeypatch):
    """Inject a fake alibabacloud FC SDK into sys.modules for all tests."""
    fake_client_module = MagicMock()
    fake_client_module.Client = MagicMock(return_value=MagicMock())

    fake_models_module = MagicMock()

    def _make_request_class(name):
        class _Req:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
            def to_map(self):
                return dict(self.__dict__)
        _Req.__name__ = name
        return _Req

    for cls_name in [
        "CreateFunctionInput",
        "CreateFunctionRequest",
        "CustomContainerConfig",
        "HeaderFieldSessionAffinityConfig",
        "GetFunctionRequest",
        "ListFunctionsRequest",
        "InvokeFunctionRequest",
        "InvokeFunctionHeaders",
    ]:
        setattr(fake_models_module, cls_name, _make_request_class(cls_name))

    fake_tea_openapi_module = MagicMock()
    fake_tea_openapi_module.models = MagicMock()
    fake_tea_openapi_module.models.Config = MagicMock()

    class _RuntimeOptions:
        def __init__(self, **kwargs):
            self.common_headers = None
            self.__dict__.update(kwargs)

    fake_tea_util_module = MagicMock()
    fake_tea_util_module.models = MagicMock()
    fake_tea_util_module.models.RuntimeOptions = _RuntimeOptions

    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330", MagicMock())
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.client", fake_client_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.models", fake_models_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi", fake_tea_openapi_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi.models", fake_tea_openapi_module.models)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util", fake_tea_util_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util.models", fake_tea_util_module.models)


# ============================================================
# IT-FC-01: FCOperatorConfig validation and defaults
# ============================================================


class TestFCOperatorConfig:
    """Integration tests for FCOperatorConfig.

    Purpose: Verify configuration class correctly validates inputs,
    provides defaults, and integrates with FCOperator.
    """

    def test_import_from_operator_module(self):
        """IT-FC-00: Verify FCOperatorConfig can be imported from operator module."""
        from rock.sandbox.operator.fc import FCOperatorConfig as ImportedConfig

        assert ImportedConfig is not None
        config = ImportedConfig()
        assert config.type == "fc"

    def test_is_pydantic_model(self):
        """IT-FC-00b: Verify FCOperatorConfig is a Pydantic BaseModel."""
        from pydantic import BaseModel

        from rock.sandbox.operator.fc import FCOperatorConfig as ImportedConfig

        assert issubclass(ImportedConfig, BaseModel)

    def test_default_values(self):
        """IT-FC-01a: Verify default configuration values."""
        config = FCOperatorConfig()

        assert config.type == "fc"
        assert config.function_name is None
        assert config.region is None
        assert config.memory is None
        assert config.cpus is None
        assert config.session_id is None
        assert config.session_ttl is None

    def test_custom_values(self):
        """IT-FC-01b: Verify custom configuration values."""
        config = FCOperatorConfig(
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

    def test_session_ttl_custom(self):
        """IT-FC-01d: Verify session_ttl can be set."""
        config = FCOperatorConfig(session_ttl=7200)
        assert config.session_ttl == 7200

    def test_session_ttl_default(self):
        """IT-FC-01e: Verify session_ttl default is None (uses FCConfig)."""
        config = FCOperatorConfig()
        assert config.session_ttl is None


# ============================================================
# IT-FC-03: FCSessionManager SDK InvokeFunction session management
# ============================================================


class TestFCSessionManager:
    """Integration tests for FCSessionManager.

    Purpose: Verify SDK InvokeFunction session creation, command execution,
    and session cleanup.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.sandbox.operator.fc import FCSessionManager

        config = FCOperatorConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
            region="cn-hangzhou",
            function_name="test-function",
        )
        return FCSessionManager(config, fc_client=_make_fc_client())

    @pytest.mark.asyncio
    async def test_create_session_invokes_function(self, session_manager):
        """IT-FC-03a: Verify create_session calls InvokeFunction with correct payload."""
        await session_manager.create_session("test-session")

        assert session_manager._fc_client.invoke_function_with_options.called
        call_args = session_manager._fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "create_session"
        assert payload["session"] == "test-session"
        assert "test-session" in session_manager.sessions

    @pytest.mark.asyncio
    async def test_create_session_passes_session_id_header(self, session_manager):
        """IT-FC-03b: Verify session affinity header is set."""
        await session_manager.create_session("test-session")

        call_args = session_manager._fc_client.invoke_function_with_options.call_args
        headers = call_args.args[2]
        assert headers.common_headers == {"x-rock-session-id": "test-session"}

    @pytest.mark.asyncio
    async def test_execute_command_invokes_function(self, session_manager):
        """IT-FC-03c: Verify execute_command sends correct payload."""
        await session_manager.create_session("test-session")

        await session_manager.execute_command(
            session_id="test-session",
            command="echo hello",
        )

        call_args = session_manager._fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "run_in_session"
        assert payload["command"] == "echo hello"

    @pytest.mark.asyncio
    async def test_close_session_invokes_function(self, session_manager):
        """IT-FC-03d: Verify close_session sends close payload."""
        await session_manager.create_session("test-session")
        await session_manager.close_session("test-session")

        # Last call should be close_session
        all_calls = session_manager._fc_client.invoke_function_with_options.call_args_list
        last_payload = json.loads(all_calls[-1].args[1].body)
        assert last_payload["action"] == "close_session"
        assert "test-session" not in session_manager.sessions

    @pytest.mark.asyncio
    async def test_is_session_alive_false_for_nonexistent(self, session_manager):
        """IT-FC-03e: Verify is_session_alive returns False for nonexistent session."""
        assert await session_manager.is_session_alive("nonexistent") is False

    @pytest.mark.asyncio
    async def test_is_session_alive_true_for_existing(self, session_manager):
        """IT-FC-03f: Verify is_session_alive returns True for existing session."""
        await session_manager.create_session("test-session")
        assert await session_manager.is_session_alive("test-session") is True

    @pytest.mark.asyncio
    async def test_get_session_stats(self, session_manager):
        """IT-FC-03g: Verify get_session_stats returns session information."""
        await session_manager.create_session("test-session")

        stats = await session_manager.get_session_stats("test-session")

        assert stats is not None
        assert stats["session_id"] == "test-session"
        assert "is_alive" in stats

    @pytest.mark.asyncio
    async def test_get_session_stats_nonexistent(self, session_manager):
        """IT-FC-03h: Verify get_session_stats returns None for nonexistent session."""
        stats = await session_manager.get_session_stats("nonexistent")
        assert stats is None


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
        from rock.sandbox.operator.fc import FCRuntime

        config = FCOperatorConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
            session_id="test-session",
        )
        return FCRuntime(config, fc_client=_make_fc_client())

    def test_runtime_initialization(self, fc_runtime):
        """IT-FC-04a: Verify runtime initializes correctly."""
        assert fc_runtime.config is not None
        assert fc_runtime.session_manager is not None
        assert fc_runtime._started is False

    @pytest.mark.asyncio
    async def test_runtime_close(self, fc_runtime):
        """IT-FC-04b: Verify runtime close cleans up resources."""
        await fc_runtime.close()
        assert fc_runtime._started is False

    @pytest.mark.asyncio
    async def test_is_alive(self, fc_runtime):
        """IT-FC-04c: Verify is_alive returns True when function responds."""
        fc_runtime.session_manager._fc_client.invoke_function_with_options.return_value.body = (
            json.dumps({"is_alive": True})
        )
        result = await fc_runtime.is_alive()
        assert result.is_alive is True

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_on_error(self, fc_runtime):
        """IT-FC-04d: Verify is_alive returns False on error."""
        fc_runtime.session_manager._fc_client.invoke_function_with_options.side_effect = (
            Exception("Connection refused")
        )
        result = await fc_runtime.is_alive()
        assert result.is_alive is False


# ============================================================
# IT-FC-08: Error handling and recovery
# ============================================================


class TestFCErrorHandling:
    """Integration tests for FC error handling.

    Purpose: Verify proper error handling and recovery mechanisms.
    """

    def test_config_missing_credentials(self):
        """IT-FC-08a: Verify config with missing credentials defaults to None."""
        config = FCOperatorConfig()

        assert config.account_id is None
        assert config.access_key_id is None
        assert config.access_key_secret is None

    @pytest.mark.asyncio
    async def test_runtime_initialization_with_config(self):
        """IT-FC-08b: Verify FCRuntime initializes correctly with config."""
        from rock.sandbox.operator.fc import FCRuntime

        config = FCOperatorConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
            function_name="test-function",
            region="cn-hangzhou",
        )
        runtime = FCRuntime(config, fc_client=_make_fc_client())

        assert runtime.config is not None
        assert runtime.session_manager is not None
        assert runtime._started is False

        await runtime.close()

    @pytest.mark.asyncio
    async def test_invoke_without_client_raises(self):
        """IT-FC-08c: Verify InvokeFunction raises when no client is set."""
        from rock.sandbox.operator.fc import FCRuntimeError, FCSessionManager

        config = FCOperatorConfig(
            account_id="test",
            access_key_id="ak",
            access_key_secret="sk",
            function_name="test-function",
            region="cn-hangzhou",
        )
        sm = FCSessionManager(config, fc_client=None)

        with pytest.raises(FCRuntimeError, match="FC SDK client not initialized"):
            await sm._invoke_function({"action": "test"})


# ============================================================
# IT-FC-09: SessionState and CircuitBreaker
# ============================================================


class TestSessionState:
    """Integration tests for SessionState.

    Purpose: Verify session state tracking functionality.
    """

    def test_default_values(self):
        """IT-FC-09a: Verify SessionState default values."""
        from rock.sandbox.operator.fc import SessionState

        state = SessionState(session_id="test-session")

        assert state.session_id == "test-session"
        assert state.ps1 == "root@fc:~$ "
        assert state.created_at > 0
        assert state.last_activity > 0

    def test_touch_updates_last_activity(self):
        """IT-FC-09b: Verify touch() updates last_activity."""
        import time

        from rock.sandbox.operator.fc import SessionState

        state = SessionState(session_id="test")
        initial_activity = state.last_activity

        time.sleep(0.01)
        state.touch()

        assert state.last_activity > initial_activity


class TestCircuitBreaker:
    """Integration tests for CircuitBreaker.

    Purpose: Verify circuit breaker state transitions.
    """

    @pytest.mark.asyncio
    async def test_starts_closed(self):
        """IT-FC-09c: Circuit breaker starts in CLOSED state."""
        from rock.sandbox.operator.fc import CircuitBreaker, CircuitState

        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert await cb.can_execute() is True

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        """IT-FC-09d: Circuit breaker opens after failure threshold."""
        from rock.sandbox.operator.fc import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            await cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert await cb.can_execute() is False

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        """IT-FC-09e: Circuit breaker transitions to HALF_OPEN after timeout."""
        from rock.sandbox.operator.fc import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.15)
        assert await cb.can_execute() is True
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_closes_after_success_in_half_open(self):
        """IT-FC-09f: Circuit breaker closes after successes in HALF_OPEN."""
        from rock.sandbox.operator.fc import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=1, success_threshold=2, recovery_timeout=0.1)
        await cb.record_failure()

        await asyncio.sleep(0.15)
        await cb.can_execute()  # transitions to HALF_OPEN

        await cb.record_success()
        await cb.record_success()

        assert cb.state == CircuitState.CLOSED



# ============================================================
# IT-FC-10: SDK retry and circuit breaker logic
# ============================================================


class TestFCSessionManagerRetry:
    """Integration tests for FCSessionManager retry logic.

    Purpose: Verify InvokeFunction retry with exponential backoff.
    """

    @pytest.fixture
    def session_manager(self):
        from rock.sandbox.operator.fc import FCSessionManager

        config = FCOperatorConfig(
            account_id="test_account",
            access_key_id="test_ak",
            access_key_secret="test_sk",
            function_name="test-function",
            region="cn-hangzhou",
        )
        return FCSessionManager(config, fc_client=_make_fc_client())

    @pytest.mark.asyncio
    async def test_retries_on_failure(self, session_manager):
        """IT-FC-10a: Verify retry on first failure, success on second."""
        response = MagicMock()
        response.body = json.dumps({"output": "ok"})

        session_manager._fc_client.invoke_function_with_options = MagicMock(
            side_effect=[Exception("Network error"), response]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await session_manager._invoke_function(
                {"action": "test"}, max_retries=2
            )

        assert result == {"output": "ok"}
        assert session_manager._fc_client.invoke_function_with_options.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, session_manager):
        """IT-FC-10b: Verify failure after max retries exhausted."""
        from rock.sandbox.operator.fc import FCRuntimeError

        session_manager._fc_client.invoke_function_with_options = MagicMock(
            side_effect=Exception("Persistent error")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(FCRuntimeError, match="failed after"):
                await session_manager._invoke_function(
                    {"action": "test"}, max_retries=2
                )

        assert session_manager._fc_client.invoke_function_with_options.call_count == 3

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, session_manager):
        """IT-FC-10c: Verify success on first attempt without retry."""
        result = await session_manager._invoke_function({"action": "test"})

        assert result == {"output": "root@fc:~$ "}
        assert session_manager._fc_client.invoke_function_with_options.call_count == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_when_open(self, session_manager):
        """IT-FC-10d: Verify circuit breaker blocks calls when open."""
        from rock.sandbox.operator.fc import FCRuntimeError

        # Force circuit breaker open
        session_manager._circuit_breaker._state = session_manager._circuit_breaker._state.__class__.OPEN
        session_manager._circuit_breaker._last_failure_time = float("inf")

        with pytest.raises(FCRuntimeError, match="Circuit breaker is OPEN"):
            await session_manager._invoke_function({"action": "test"})

    @pytest.mark.asyncio
    async def test_record_success_resets_failures(self, session_manager):
        """IT-FC-10e: Verify successful call resets failure count."""
        # Record some failures first
        await session_manager._circuit_breaker.record_failure()
        await session_manager._circuit_breaker.record_failure()
        assert session_manager._circuit_breaker._failure_count == 2

        # Successful call resets
        await session_manager._invoke_function({"action": "test"})
        assert session_manager._circuit_breaker._failure_count == 0
