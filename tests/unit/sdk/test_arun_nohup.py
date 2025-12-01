import types

import pytest

from rock.actions.sandbox.response import Observation
from rock.sdk.common.constants import PID_PREFIX, PID_SUFFIX
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


@pytest.mark.asyncio
async def test_arun_nohup_collect_output_false_returns_hint(monkeypatch):
    timestamp = 1701
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    executed_commands: list[str] = []

    async def fake_run_in_session(self, action):
        executed_commands.append(action.command)
        if action.command.startswith("nohup "):
            return Observation(output=f"{PID_PREFIX}12345{PID_SUFFIX}", exit_code=0)
        if action.command.startswith("stat "):
            # Return a mock file size of 2048 bytes
            return Observation(output="2048", exit_code=0)
        raise AssertionError(f"Unexpected command executed: {action.command}")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return True, "Process completed successfully in 1.0s"

    monkeypatch.setattr(Sandbox, "_wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="echo detached",
        session="bash-detached",
        mode="nohup",
        collect_output=False,
    )

    assert result.exit_code == 0
    assert result.failure_reason == ""
    assert "/tmp/tmp_1701.out" in result.output
    assert "without streaming the log content" in result.output
    assert "File size: 2.00 KB" in result.output
    assert len(executed_commands) == 2
    assert executed_commands[0].startswith("nohup ")
    assert executed_commands[1].startswith("stat ")


@pytest.mark.asyncio
async def test_arun_nohup_collect_output_false_propagates_failure(monkeypatch):
    timestamp = 1802
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    executed_commands: list[str] = []

    async def fake_run_in_session(self, action):
        executed_commands.append(action.command)
        if action.command.startswith("nohup "):
            return Observation(output=f"{PID_PREFIX}999{PID_SUFFIX}", exit_code=0)
        if action.command.startswith("stat "):
            # Return a mock file size of 512 bytes
            return Observation(output="512", exit_code=0)
        raise AssertionError("Unexpected command execution when collect_output=False")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return False, "Process timed out"

    monkeypatch.setattr(Sandbox, "_wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="sleep 999",
        session="bash-detached",
        mode="nohup",
        collect_output=False,
    )

    assert result.exit_code == 1
    assert result.failure_reason == "Process timed out"
    assert "Process timed out" in result.output
    assert "/tmp/tmp_1802.out" in result.output
    assert "File size: 512 bytes" in result.output
    assert len(executed_commands) == 2

