"""Tests for DockerHealthTask."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.docker_health_task import DockerHealthTask


class _FakeTaskConfig:
    def __init__(self, interval_seconds=1800):
        self.params = {}
        self.interval_seconds = interval_seconds


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout=""):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(side_effect):
    """Create a mock runtime whose execute returns results from *side_effect* in order."""
    rt = AsyncMock()
    rt._config = type("C", (), {"host": "10.0.0.1"})()
    rt.execute = AsyncMock(side_effect=side_effect)
    return rt


class TestInit:
    def test_defaults(self):
        task = DockerHealthTask()
        assert task.type == "docker_health"
        assert task.interval_seconds == 60

    def test_custom_interval(self):
        task = DockerHealthTask(interval_seconds=1800)
        assert task.interval_seconds == 1800


class TestFromConfig:
    def test_from_config(self):
        task = DockerHealthTask.from_config(_FakeTaskConfig(interval_seconds=3600))
        assert task.interval_seconds == 3600


class TestRunAction:
    @pytest.mark.asyncio
    async def test_docker_healthy_skips_restart(self):
        rt = _runtime([_FakeExecResult(exit_code=0)])
        task = DockerHealthTask()

        result = await task.run_action(rt)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["restarted"] is False
        assert "checked_at" in result
        assert rt.execute.await_count == 1
        probe_cmd = rt.execute.await_args_list[0].args[0]
        assert probe_cmd.command == "docker info"

    @pytest.mark.asyncio
    async def test_docker_down_triggers_restart(self):
        rt = _runtime(
            [
                _FakeExecResult(exit_code=1),  # probe fails
                _FakeExecResult(exit_code=0),  # restart succeeds
            ]
        )
        task = DockerHealthTask()

        result = await task.run_action(rt)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["restarted"] is True
        assert result["restart_exit_code"] == 0
        assert rt.execute.await_count == 2
        restart_cmd = rt.execute.await_args_list[1].args[0]
        assert restart_cmd.command == "sudo service docker start"

    @pytest.mark.asyncio
    async def test_docker_down_restart_also_fails(self):
        rt = _runtime(
            [
                _FakeExecResult(exit_code=1),
                _FakeExecResult(exit_code=3),
            ]
        )
        task = DockerHealthTask()

        result = await task.run_action(rt)

        assert result["restarted"] is True
        assert result["restart_exit_code"] == 3
