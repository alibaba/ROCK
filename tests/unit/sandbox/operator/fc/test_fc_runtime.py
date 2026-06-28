"""Unit tests for FCRuntime / FCSessionManager.

Verifies the FC SDK InvokeFunction session model:
- _invoke_function correctly calls FC SDK with payload and session affinity header
- Session lifecycle (create / run / close) via InvokeFunction
- Stateless operations (execute / read_file / write_file / is_alive)
- Retry and circuit breaker behavior
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.sandbox.operator.fc.runtime import (
    CircuitState,
    FCRuntime,
    FCRuntimeError,
    FCSessionManager,
)

# ---------------------------------------------------------------------------
# _invoke_function: SDK call contract
# ---------------------------------------------------------------------------


class TestInvokeFunction:
    """Verify _invoke_function correctly calls the FC SDK."""

    async def test_invokes_with_correct_function_name(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm._invoke_function({"action": "is_alive"})

        mock_fc_client.invoke_function_with_options.assert_called_once()
        call_args = mock_fc_client.invoke_function_with_options.call_args
        assert call_args.args[0] == fc_operator_config.function_name

    async def test_payload_is_json_serialized(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        payload = {"action": "create_session", "session": "sid-1"}
        await sm._invoke_function(payload)

        call_args = mock_fc_client.invoke_function_with_options.call_args
        request = call_args.args[1]
        assert json.loads(request.body) == payload

    async def test_session_id_sets_common_headers(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm._invoke_function({"action": "run"}, session_id="sid-affinity")

        call_args = mock_fc_client.invoke_function_with_options.call_args
        headers = call_args.args[2]
        assert hasattr(headers, "common_headers")
        assert headers.common_headers == {"x-rock-session-id": "sid-affinity"}

    async def test_no_session_id_no_common_headers(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm._invoke_function({"action": "execute"})

        call_args = mock_fc_client.invoke_function_with_options.call_args
        headers = call_args.args[2]
        # common_headers should not be set (or be falsy) when no session_id
        common_headers = getattr(headers, "common_headers", None)
        assert not common_headers

    async def test_response_body_parsed_as_json(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"output": "hello", "exit_code": 0}
        )

        result = await sm._invoke_function({"action": "run"})
        assert result == {"output": "hello", "exit_code": 0}

    async def test_raises_without_client(self, fc_operator_config):
        sm = FCSessionManager(fc_operator_config, fc_client=None)
        with pytest.raises(FCRuntimeError, match="FC SDK client not initialized"):
            await sm._invoke_function({"action": "is_alive"})

    async def test_retries_on_failure(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk, monkeypatch
    ):
        # Fail twice, succeed on third attempt
        good_response = MagicMock()
        good_response.body = json.dumps({"ok": True})
        mock_fc_client.invoke_function_with_options.side_effect = [
            RuntimeError("network error"),
            RuntimeError("timeout"),
            good_response,
        ]

        # Patch asyncio.sleep to avoid real delays
        from rock.sandbox.operator.fc import runtime as rt_module
        monkeypatch.setattr(rt_module.asyncio, "sleep", AsyncMock())

        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        result = await sm._invoke_function({"action": "run"}, max_retries=2)

        assert result == {"ok": True}
        assert mock_fc_client.invoke_function_with_options.call_count == 3

    async def test_raises_after_max_retries(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk, monkeypatch
    ):
        mock_fc_client.invoke_function_with_options.side_effect = RuntimeError("permanent error")

        from rock.sandbox.operator.fc import runtime as rt_module
        monkeypatch.setattr(rt_module.asyncio, "sleep", AsyncMock())

        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        with pytest.raises(FCRuntimeError, match="InvokeFunction failed after"):
            await sm._invoke_function({"action": "run"}, max_retries=1)

        assert mock_fc_client.invoke_function_with_options.call_count == 2

    async def test_circuit_breaker_blocks_when_open(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        # Force circuit breaker into OPEN state
        sm._circuit_breaker._state = CircuitState.OPEN
        sm._circuit_breaker._last_failure_time = float("inf")  # Never recover

        with pytest.raises(FCRuntimeError, match="Circuit breaker is OPEN"):
            await sm._invoke_function({"action": "is_alive"})

        mock_fc_client.invoke_function_with_options.assert_not_called()


# ---------------------------------------------------------------------------
# Session lifecycle via InvokeFunction
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Verify create / run / close session use _invoke_function with correct payloads."""

    async def test_create_session_calls_invoke_with_action(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"output": "root@fc:~$ "}
        )

        result = await sm.create_session("sid-1")

        call_args = mock_fc_client.invoke_function_with_options.call_args
        request = call_args.args[1]
        payload = json.loads(request.body)
        assert payload["action"] == "create_session"
        assert payload["session"] == "sid-1"
        assert payload["session_type"] == "bash"
        assert result == {"output": "root@fc:~$ "}

    async def test_create_session_stores_state(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm.create_session("sid-stored", ps1="custom$ ")

        assert "sid-stored" in sm.sessions
        assert sm.sessions["sid-stored"].ps1 == "custom$ "

    async def test_create_session_passes_session_id_for_affinity(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm.create_session("sid-affinity")

        call_args = mock_fc_client.invoke_function_with_options.call_args
        headers = call_args.args[2]
        assert headers.common_headers == {"x-rock-session-id": "sid-affinity"}

    async def test_create_session_closes_existing_first(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        # Pre-populate a session
        sm.sessions["sid-existing"] = MagicMock()

        await sm.create_session("sid-existing")

        # Should have been called at least twice (close + create)
        assert mock_fc_client.invoke_function_with_options.call_count >= 2

    async def test_execute_command_calls_invoke_with_action(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        sm.sessions["sid-run"] = MagicMock()

        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"output": "hello world", "exit_code": 0}
        )

        output, exit_code = await sm.execute_command("sid-run", "echo hello")

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "run_in_session"
        assert payload["command"] == "echo hello"
        assert output == "hello world"
        assert exit_code == 0

    async def test_execute_command_raises_for_unknown_session(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        with pytest.raises(ValueError, match="not found"):
            await sm.execute_command("nonexistent", "echo test")

    async def test_close_session_calls_invoke_with_action(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        sm.sessions["sid-close"] = MagicMock()

        await sm.close_session("sid-close")

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "close_session"

    async def test_close_session_removes_from_sessions(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        sm.sessions["sid-remove"] = MagicMock()

        await sm.close_session("sid-remove")

        assert "sid-remove" not in sm.sessions

    async def test_close_session_unknown_session_noop(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        sm = FCSessionManager(fc_operator_config, fc_client=mock_fc_client)
        await sm.close_session("nonexistent")
        mock_fc_client.invoke_function_with_options.assert_not_called()

    async def test_is_session_alive(self, fc_operator_config):
        sm = FCSessionManager(fc_operator_config)
        sm.sessions["sid-alive"] = MagicMock()

        assert await sm.is_session_alive("sid-alive") is True
        assert await sm.is_session_alive("sid-dead") is False


# ---------------------------------------------------------------------------
# FCRuntime stateless operations
# ---------------------------------------------------------------------------


class TestStatelessOperations:
    """Verify execute / read_file / write_file / is_alive via InvokeFunction."""

    async def test_execute_no_session_id(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"stdout": "output", "stderr": "", "exit_code": 0}
        )

        from rock.actions import Command
        result = await runtime.execute(Command(command="echo test", timeout=30, shell=True))

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "execute"
        assert payload["command"] == "echo test"

        # No session_id for stateless execute
        headers = call_args.args[2]
        assert not getattr(headers, "common_headers", None)

        assert result.stdout == "output"
        assert result.exit_code == 0

    async def test_is_alive_no_session_id(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"is_alive": True}
        )

        result = await runtime.is_alive()

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "is_alive"

        headers = call_args.args[2]
        assert not getattr(headers, "common_headers", None)

        assert result.is_alive is True

    async def test_is_alive_returns_false_on_error(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.side_effect = RuntimeError("network down")

        result = await runtime.is_alive()
        assert result.is_alive is False

    async def test_read_file_uses_session_id(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"content": "file content"}
        )

        from rock.actions import ReadFileRequest
        result = await runtime.read_file(ReadFileRequest(path="/tmp/test.txt"))

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "read_file"
        assert payload["path"] == "/tmp/test.txt"

        # Session ID should be set for session affinity
        headers = call_args.args[2]
        assert headers.common_headers == {"x-rock-session-id": fc_operator_config.session_id}

        assert result.content == "file content"

    async def test_write_file_uses_session_id(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"success": True}
        )

        from rock.actions import WriteFileRequest
        await runtime.write_file(
            WriteFileRequest(path="/tmp/test.txt", content="hello")
        )

        call_args = mock_fc_client.invoke_function_with_options.call_args
        payload = json.loads(call_args.args[1].body)
        assert payload["action"] == "write_file"
        assert payload["content"] == "hello"

        headers = call_args.args[2]
        assert headers.common_headers == {"x-rock-session-id": fc_operator_config.session_id}

    async def test_create_session_returns_output(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"output": "root@fc:~$ "}
        )

        from rock.actions import CreateSessionRequest
        result = await runtime.create_session(CreateSessionRequest(session="sid-new"))

        assert result.output == "root@fc:~$ "

    async def test_run_in_session_returns_observation(
        self, fc_operator_config, mock_fc_client, fake_fc_sdk
    ):
        runtime = FCRuntime(fc_operator_config, fc_client=mock_fc_client)
        # Pre-populate session state
        runtime.session_manager.sessions["sid-run"] = MagicMock()
        mock_fc_client.invoke_function_with_options.return_value.body = json.dumps(
            {"output": "hello", "exit_code": 0}
        )

        from rock.actions.sandbox.request import Action
        action = Action(session="sid-run", command="echo hello", action_type="bash")
        result = await runtime.run_in_session(action)

        assert result.output == "hello"
        assert result.exit_code == 0
