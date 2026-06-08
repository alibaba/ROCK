"""Regression test for RemoteSandboxRuntime.check_pid_exists.

PR #985 added ``NonBlankStr sandbox_id`` to ``SandboxCommand``, so the
caller-supplied ``sandbox_id`` must reach the wire request -- otherwise
construction raises ``pydantic.ValidationError`` (breaking the scheduler's
non-idempotent task cleanup path ``task_base.cleanup_on_worker`` ->
``runtime.check_pid_exists``).
"""

from unittest.mock import AsyncMock

import pytest

from rock.actions import CommandResponse
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime


@pytest.mark.asyncio
async def test_check_pid_exists_forwards_sandbox_id_to_command():
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="exists\n", stderr=""))

    assert await runtime.check_pid_exists(1234, sandbox_id="scheduler-task") is True

    cmd_arg = runtime.execute.call_args.args[0]
    assert cmd_arg.sandbox_id == "scheduler-task"


@pytest.mark.asyncio
async def test_check_pid_exists_without_process_name_uses_kill_only():
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="exists\n", stderr=""))

    result = await runtime.check_pid_exists(1234, sandbox_id="scheduler-task")

    assert result is True
    cmd = runtime.execute.call_args.args[0].command
    assert "kill -0 1234" in cmd
    assert "/proc/" not in cmd


@pytest.mark.asyncio
async def test_check_pid_exists_with_process_name_verifies_cmdline():
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="exists\n", stderr=""))

    result = await runtime.check_pid_exists(8959, sandbox_id="scheduler-task", process_name="docuum")

    assert result is True
    cmd = runtime.execute.call_args.args[0].command
    assert "kill -0 8959" in cmd
    assert "/proc/8959/cmdline" in cmd
    assert "docuum" in cmd


@pytest.mark.asyncio
async def test_check_pid_exists_with_process_name_rejects_wrong_process():
    """PID exists but belongs to a different process (e.g. containerd thread reusing TID)."""
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="not_exists\n", stderr=""))

    result = await runtime.check_pid_exists(8959, sandbox_id="scheduler-task", process_name="docuum")

    assert result is False


@pytest.mark.asyncio
async def test_check_pid_exists_with_process_name_not_exists():
    """PID does not exist at all (kill -0 fails)."""
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="not_exists\n", stderr=""))

    result = await runtime.check_pid_exists(9999, sandbox_id="scheduler-task", process_name="docuum")

    assert result is False
