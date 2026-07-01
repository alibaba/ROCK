"""Tests for DockerHealthTask."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import IdempotencyType, TaskStatusEnum
from rock.admin.scheduler.tasks.docker_health_task import DockerHealthTask


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout=""):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(probe_exit_code=0, restart_exit_code=0, host="10.0.0.1"):
    rt = AsyncMock()
    rt._config = type("C", (), {"host": host})()
    rt.execute = AsyncMock(
        side_effect=[
            _FakeExecResult(exit_code=probe_exit_code),
            _FakeExecResult(exit_code=restart_exit_code),
        ]
    )
    return rt


def test_is_idempotent():
    task = DockerHealthTask()
    assert task.type == "docker_health"
    assert task.idempotency == IdempotencyType.IDEMPOTENT


@pytest.mark.asyncio
async def test_docker_alive_no_restart():
    task = DockerHealthTask()
    runtime = _runtime(probe_exit_code=0)

    result = await task.run_action(runtime)

    assert result["status"] == TaskStatusEnum.SUCCESS
    assert result["restarted"] is False
    assert "checked_at" in result
    # Only the probe ran, no restart.
    assert runtime.execute.await_count == 1
    assert runtime.execute.await_args_list[0].args[0].command == "docker info"


@pytest.mark.asyncio
async def test_docker_down_triggers_restart():
    task = DockerHealthTask()
    runtime = _runtime(probe_exit_code=1, restart_exit_code=0)

    result = await task.run_action(runtime)

    assert result["status"] == TaskStatusEnum.SUCCESS
    assert result["restarted"] is True
    assert result["restart_exit_code"] == 0
    assert "checked_at" in result
    assert runtime.execute.await_count == 2
    assert runtime.execute.await_args_list[1].args[0].command == "sudo service docker start"
