"""SDK operations honor environment timing defaults and explicit overrides."""

from unittest.mock import AsyncMock

import pytest

from rock.actions import Observation, SandboxStatusResponse
from rock.common.constants import PID_PREFIX, PID_SUFFIX
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


@pytest.mark.parametrize("operation", ["start", "restart"])
@pytest.mark.parametrize("value, expected", [(None, 3), ("0.25", 0.25)])
async def test_status_polling_interval_from_environment(monkeypatch, operation, value, expected):
    if value is None:
        monkeypatch.delenv("ROCK_SANDBOX_STATUS_CHECK_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ROCK_SANDBOX_STATUS_CHECK_INTERVAL_SECONDS", value)
    sandbox = Sandbox(SandboxConfig())
    sandbox._sandbox_id = "test-sandbox"
    monkeypatch.setattr(
        "rock.sdk.sandbox.client.HttpUtils.post",
        AsyncMock(return_value={"status": "Success", "result": {"sandbox_id": "test-sandbox"}}),
    )
    monkeypatch.setattr(
        sandbox,
        "get_status",
        AsyncMock(side_effect=[SandboxStatusResponse(is_alive=False), SandboxStatusResponse(is_alive=True)]),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("rock.sdk.sandbox.client.asyncio.sleep", sleep)

    await getattr(sandbox, operation)()

    sleep.assert_awaited_once_with(expected)


@pytest.mark.parametrize("operation", ["arun", "run_nohup_and_wait"])
@pytest.mark.parametrize("explicit", [None, (60, 2), (0, 0)])
@pytest.mark.parametrize("configured", [False, True])
async def test_command_wait_default_precedence(monkeypatch, operation, explicit, configured):
    for variable, value in [("ROCK_SANDBOX_WAIT_TIMEOUT_SECONDS", "40"), ("ROCK_SANDBOX_WAIT_INTERVAL_SECONDS", "7")]:
        if configured:
            monkeypatch.setenv(variable, value)
        else:
            monkeypatch.delenv(variable, raising=False)
    sandbox = Sandbox(SandboxConfig())
    monkeypatch.setattr(sandbox, "create_session", AsyncMock())
    monkeypatch.setattr(
        sandbox,
        "_run_in_session",
        AsyncMock(return_value=Observation(output=f"{PID_PREFIX}123{PID_SUFFIX}", exit_code=0)),
    )
    wait = AsyncMock(return_value=(True, "done"))
    monkeypatch.setattr(sandbox, "wait_for_process_completion", wait)
    monkeypatch.setattr(sandbox, "handle_nohup_output", AsyncMock(return_value=Observation(output="done", exit_code=0)))
    kwargs = {"wait_timeout": explicit[0], "wait_interval": explicit[1]} if explicit is not None else {}
    if operation == "arun":
        kwargs.update(mode="nohup", session="test-session")

    await getattr(sandbox, operation)("echo hello", **kwargs)

    expected = explicit if explicit is not None else ((40, 7) if configured else (300, 10))
    assert wait.await_args.kwargs["wait_timeout"] == expected[0]
    assert wait.await_args.kwargs["wait_interval"] == expected[1]


@pytest.mark.parametrize("operation", ["start_nohup_process", "run_nohup_and_wait"])
@pytest.mark.parametrize("value, expected", [(None, 30), ("17.5", 17.5)])
async def test_nohup_submission_timeout_from_environment(monkeypatch, operation, value, expected):
    if value is None:
        monkeypatch.delenv("ROCK_SANDBOX_NOHUP_COMMAND_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ROCK_SANDBOX_NOHUP_COMMAND_TIMEOUT_SECONDS", value)
    sandbox = Sandbox(SandboxConfig())
    run = AsyncMock(return_value=Observation(output=f"{PID_PREFIX}123{PID_SUFFIX}", exit_code=0))
    monkeypatch.setattr(sandbox, "_run_in_session", run)
    monkeypatch.setattr(sandbox, "create_session", AsyncMock())
    monkeypatch.setattr(sandbox, "wait_for_process_completion", AsyncMock(return_value=(True, "done")))

    if operation == "start_nohup_process":
        await sandbox.start_nohup_process("echo hello", "/tmp/test.out", "test-session")
    else:
        await sandbox.run_nohup_and_wait("echo hello")

    assert run.await_args.args[0].timeout == expected


@pytest.mark.parametrize("minimum, auto_clear, expected", [(None, 300, 5), ("1", 300, 2), ("7", 300, 7), ("7", 6, 4)])
async def test_minimum_poll_interval_preserves_cleanup_cap(monkeypatch, minimum, auto_clear, expected):
    if minimum is None:
        monkeypatch.delenv("ROCK_SANDBOX_MIN_WAIT_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ROCK_SANDBOX_MIN_WAIT_INTERVAL_SECONDS", minimum)
    sandbox = Sandbox(SandboxConfig(auto_clear_seconds=auto_clear))
    monkeypatch.setattr(sandbox, "_run_in_session", AsyncMock(return_value=Observation(output="", exit_code=0)))

    class StopPolling(Exception):
        pass

    sleep = AsyncMock(side_effect=StopPolling)
    monkeypatch.setattr("rock.sdk.sandbox.client.asyncio.sleep", sleep)

    with pytest.raises(StopPolling):
        await sandbox.wait_for_process_completion(123, "test-session", wait_timeout=60, wait_interval=2)

    sleep.assert_awaited_once_with(expected)
