"""Tests for RayLogCleanupTask."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.ray_log_cleanup_task import RayLogCleanupTask


class _FakeTaskConfig:
    def __init__(self, params=None, interval_seconds=86400):
        self.params = params or {}
        self.interval_seconds = interval_seconds


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout="ray_log_cleanup_done"):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(stdout="ray_log_cleanup_done", exit_code=0):
    rt = AsyncMock()
    rt._config = type("C", (), {"host": "10.0.0.1"})()
    rt.execute = AsyncMock(return_value=_FakeExecResult(exit_code=exit_code, stdout=stdout))
    return rt


class TestInit:
    def test_default(self):
        task = RayLogCleanupTask()
        assert task.type == "ray_log_cleanup"
        assert task.ray_temp_dir == "/data/tmp/ray"
        assert task.min_age_hours == 24

    def test_strips_trailing_slash(self):
        task = RayLogCleanupTask(ray_temp_dir="/data/ray/")
        assert task.ray_temp_dir == "/data/ray"

    def test_rejects_min_age_below_one(self):
        with pytest.raises(ValueError, match="min_age_hours must be >= 1"):
            RayLogCleanupTask(min_age_hours=0)


class TestFromConfig:
    def test_from_config_defaults(self):
        task = RayLogCleanupTask.from_config(_FakeTaskConfig())
        assert task.ray_temp_dir == "/data/tmp/ray"
        assert task.min_age_hours == 24

    def test_from_config_custom(self):
        cfg = _FakeTaskConfig(
            params={"ray_temp_dir": "/data/ray", "min_age_hours": 48},
            interval_seconds=3600,
        )
        task = RayLogCleanupTask.from_config(cfg)
        assert task.ray_temp_dir == "/data/ray"
        assert task.min_age_hours == 48
        assert task.interval_seconds == 3600


class TestRunAction:
    @pytest.mark.asyncio
    async def test_command_skips_session_latest(self):
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # `! -name "session_latest"` skips the symlink itself; readlink resolves
        # the target so we also skip whichever real session it points at.
        assert '! -name "session_latest"' in cmd
        assert "readlink" in cmd
        assert 'name "session_*"' in cmd

    @pytest.mark.asyncio
    async def test_command_uses_min_age_in_minutes(self):
        task = RayLogCleanupTask(min_age_hours=48)
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # 48h * 60 = 2880
        assert "-mmin +2880" in cmd

    @pytest.mark.asyncio
    async def test_command_respects_custom_temp_dir(self):
        task = RayLogCleanupTask(ray_temp_dir="/data/ray")
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        assert '"/data/ray"' in cmd

    @pytest.mark.asyncio
    async def test_extracts_removed_count_from_output(self):
        stdout = (
            "live_session=session_2026_03_01_xyz_111\n"
            "removed=session_2026_02_15_aaa_222\n"
            "removed=session_2026_02_20_bbb_333\n"
            "ray_log_cleanup_done"
        )
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["removed_count"] == 2
        assert "session_2026_02_15_aaa_222" in result["removed_sessions"]
        assert "session_2026_02_20_bbb_333" in result["removed_sessions"]

    @pytest.mark.asyncio
    async def test_handles_missing_ray_dir(self):
        task = RayLogCleanupTask()
        runtime = _runtime(stdout="ray_temp_dir_not_found")

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["removed_count"] == 0
