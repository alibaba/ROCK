import types

import pytest
from httpx import ReadTimeout

from rock.actions.sandbox.response import Observation
from rock.common.constants import PID_PREFIX, PID_SUFFIX
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


@pytest.mark.asyncio
async def test_arun_normal_mode_without_session_creates_temp_session(monkeypatch):
    """When arun is called in NORMAL mode without a session, it should auto-create one."""
    timestamp = 3001
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    created_sessions: list[str] = []
    captured_session = None

    async def fake_create_session(self, request):
        created_sessions.append(request.session)

    async def fake_run_in_session(self, action):
        nonlocal captured_session
        captured_session = action.session
        return Observation(output="ok", exit_code=0)

    sandbox.create_session = types.MethodType(fake_create_session, sandbox)  # type: ignore
    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    result = await sandbox.arun(cmd="ls -la", mode="normal")

    assert result.exit_code == 0
    assert result.output == "ok"
    assert len(created_sessions) == 1
    assert created_sessions[0] == f"bash-{timestamp}"
    assert captured_session == f"bash-{timestamp}"


@pytest.mark.asyncio
async def test_arun_normal_mode_with_session_does_not_create_new_session(monkeypatch):
    """When arun is called in NORMAL mode with an existing session, it should NOT create a new one."""
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    session_created = False

    async def fake_create_session(self, request):
        nonlocal session_created
        session_created = True

    async def fake_run_in_session(self, action):
        return Observation(output="ok", exit_code=0)

    sandbox.create_session = types.MethodType(fake_create_session, sandbox)  # type: ignore
    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    await sandbox.arun(cmd="ls -la", session="bash-existing", mode="normal")

    assert not session_created


@pytest.mark.asyncio
async def test_arun_nohup_ignore_output_true_returns_hint(monkeypatch):
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

    monkeypatch.setattr(Sandbox, "wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="echo detached",
        session="bash-detached",
        mode="nohup",
        ignore_output=True,
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
async def test_arun_nohup_ignore_output_true_propagates_failure(monkeypatch):
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
        raise AssertionError("Unexpected command execution when ignore_output=True")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return False, "Process timed out"

    monkeypatch.setattr(Sandbox, "wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="sleep 999",
        session="bash-detached",
        mode="nohup",
        ignore_output=True,
    )

    assert result.exit_code == 1
    assert result.failure_reason == "Process timed out"
    assert "Process timed out" in result.output
    assert "/tmp/tmp_1802.out" in result.output
    assert "File size: 512 bytes" in result.output
    assert len(executed_commands) == 2


@pytest.mark.asyncio
async def test_arun_nohup_ignore_output_stat_fails(monkeypatch):
    timestamp = 1903
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    executed_commands: list[str] = []

    async def fake_run_in_session(self, action):
        executed_commands.append(action.command)
        if action.command.startswith("nohup "):
            return Observation(output=f"{PID_PREFIX}222{PID_SUFFIX}", exit_code=0)
        if action.command.startswith("stat "):
            # Simulate stat failure / non-digit output
            return Observation(output="n/a", exit_code=1)
        raise AssertionError("Unexpected command execution when ignore_output=True")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return True, "Process completed"

    monkeypatch.setattr(Sandbox, "wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="echo ignore",
        session="bash-detached",
        mode="nohup",
        ignore_output=True,
    )

    assert result.exit_code == 0
    assert "File size:" not in result.output  # stat failed, size omitted
    assert "/tmp/tmp_1903.out" in result.output
    assert len(executed_commands) == 2


@pytest.mark.asyncio
async def test_arun_nohup_pid_extract_fail(monkeypatch):
    timestamp = 2001
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    async def fake_run_in_session(self, action):
        if action.command.startswith("nohup "):
            return Observation(output="NO_PID_OUTPUT", exit_code=0)
        raise AssertionError("Unexpected command execution when PID missing")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    result = await sandbox.arun(
        cmd="echo nopid",
        session="bash-detached",
        mode="nohup",
        ignore_output=True,
    )

    assert result.exit_code == 1
    assert "Failed to submit command" in result.failure_reason
    assert "Failed to submit command" in result.output


@pytest.mark.asyncio
async def test_arun_nohup_read_timeout(monkeypatch):
    timestamp = 2101
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    async def fake_run_in_session(self, action):
        # Simulate timeout on submitting nohup command
        raise ReadTimeout("timeout")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    result = await sandbox.arun(
        cmd="sleep 1",
        session="bash-detached",
        mode="nohup",
    )

    assert result.exit_code == 1
    assert "timeout" in result.output
    assert "timeout" in result.failure_reason


@pytest.mark.asyncio
async def test_arun_nohup_response_limited(monkeypatch):
    timestamp = 2201
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    executed_commands: list[str] = []

    async def fake_run_in_session(self, action):
        executed_commands.append(action.command)
        if action.command.startswith("nohup "):
            return Observation(output=f"{PID_PREFIX}555{PID_SUFFIX}", exit_code=0)
        if action.command.startswith("head -c 5"):
            return Observation(output="hello", exit_code=0)
        raise AssertionError(f"Unexpected command executed: {action.command}")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return True, "done"

    monkeypatch.setattr(Sandbox, "wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="echo long_output",
        session="bash-detached",
        mode="nohup",
        response_limited_bytes_in_nohup=5,
    )

    assert result.exit_code == 0
    assert result.output == "hello"
    assert any(cmd.startswith("head -c 5") for cmd in executed_commands)


@pytest.mark.asyncio
async def test_arun_nohup_default_collects_output(monkeypatch):
    timestamp = 2301
    monkeypatch.setattr("rock.sdk.sandbox.client.time.time_ns", lambda: timestamp)
    sandbox = Sandbox(SandboxConfig(image="mock-image"))

    executed_commands: list[str] = []

    async def fake_run_in_session(self, action):
        executed_commands.append(action.command)
        if action.command.startswith("nohup "):
            return Observation(output=f"{PID_PREFIX}777{PID_SUFFIX}", exit_code=0)
        if action.command.startswith("cat "):
            return Observation(output="full-log", exit_code=0)
        raise AssertionError(f"Unexpected command executed: {action.command}")

    sandbox._run_in_session = types.MethodType(fake_run_in_session, sandbox)  # type: ignore

    async def fake_wait(self, pid, session, wait_timeout, wait_interval):
        return True, "done"

    monkeypatch.setattr(Sandbox, "wait_for_process_completion", fake_wait)

    result = await sandbox.arun(
        cmd="echo default",
        session="bash-detached",
        mode="nohup",
    )

    assert result.exit_code == 0
    assert result.output == "full-log"
    assert any(cmd.startswith("cat ") for cmd in executed_commands)
