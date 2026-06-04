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
