"""Regression test for RemoteSandboxRuntime.check_pid_exists.

PR #985 added ``NonBlankStr sandbox_id`` to ``SandboxCommand``. Constructing
``Command(command=..., shell=True)`` without ``sandbox_id`` now raises
``pydantic.ValidationError`` at construction time, breaking the scheduler's
non-idempotent task cleanup path (``task_base.cleanup_on_worker`` ->
``runtime.check_pid_exists``).
"""

from unittest.mock import AsyncMock

import pytest

from rock.actions import CommandResponse
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime


@pytest.mark.asyncio
async def test_check_pid_exists_constructs_command_with_non_blank_sandbox_id():
    runtime = RemoteSandboxRuntime(host="http://127.0.0.1", port=22555)
    runtime.execute = AsyncMock(return_value=CommandResponse(exit_code=0, stdout="exists\n", stderr=""))

    # If Command construction raises ValidationError, this call will propagate it.
    assert await runtime.check_pid_exists(1234) is True

    cmd_arg = runtime.execute.call_args.args[0]
    assert cmd_arg.sandbox_id and cmd_arg.sandbox_id.strip(), (
        "SandboxCommand.sandbox_id is NonBlankStr; check_pid_exists must construct it with a non-blank placeholder"
    )
