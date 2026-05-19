"""Tests for ImageCleanupTask (docuum LRU + dangling/BuildKit prune)."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.image_cleanup_task import ImageCleanupTask


class _FakeTaskConfig:
    def __init__(self, params=None, interval_seconds=3600):
        self.params = params or {}
        self.interval_seconds = interval_seconds


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout=""):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(side_effects):
    """Build a runtime whose execute() returns successive results from side_effects."""
    rt = AsyncMock()
    rt._config = type("C", (), {"host": "10.0.0.1"})()
    rt.execute = AsyncMock(side_effect=side_effects)
    return rt


# Fixed call sequence for run_action when keep_build_storage is set:
#   1) docuum install check
#   2) docuum start (returns PID-tagged stdout)
#   3) docker image prune + docker builder prune (one combined cmd)
def _default_results(pid=12345, prune_stdout="Total reclaimed space: 1.2GB"):
    return [
        _FakeExecResult(),
        _FakeExecResult(stdout=f"PIDSTART{pid}PIDEND"),
        _FakeExecResult(stdout=prune_stdout),
    ]


class TestInit:
    def test_default(self):
        task = ImageCleanupTask()
        assert task.type == "image_cleanup"
        assert task.interval_seconds == 3600
        assert task.disk_threshold == "1T"
        assert task.image_whitelist == []
        assert task.keep_build_storage == "20GB"

    def test_custom(self):
        task = ImageCleanupTask(
            interval_seconds=600,
            disk_threshold="500G",
            image_whitelist=[r"^rock-base.*$"],
            keep_build_storage="5GB",
        )
        assert task.interval_seconds == 600
        assert task.disk_threshold == "500G"
        assert task.image_whitelist == [r"^rock-base.*$"]
        assert task.keep_build_storage == "5GB"

    def test_disable_prune(self):
        task = ImageCleanupTask(keep_build_storage=None)
        assert task.keep_build_storage is None


class TestFromConfig:
    def test_from_config_defaults(self):
        task = ImageCleanupTask.from_config(_FakeTaskConfig())
        assert task.disk_threshold == "1T"
        assert task.image_whitelist == []
        assert task.keep_build_storage == "20GB"

    def test_from_config_custom(self):
        cfg = _FakeTaskConfig(
            params={
                "disk_threshold": "200G",
                "image_whitelist": [r"^pinned:.*$"],
                "keep_build_storage": "10GB",
            },
            interval_seconds=7200,
        )
        task = ImageCleanupTask.from_config(cfg)
        assert task.interval_seconds == 7200
        assert task.disk_threshold == "200G"
        assert task.image_whitelist == [r"^pinned:.*$"]
        assert task.keep_build_storage == "10GB"


class TestRunAction:
    @pytest.mark.asyncio
    async def test_docuum_command_includes_threshold(self):
        task = ImageCleanupTask(disk_threshold="500G")
        runtime = _runtime(_default_results())

        await task.run_action(runtime)
        docuum_cmd = runtime.execute.await_args_list[1].args[0].command
        assert "docuum --threshold 500G" in docuum_cmd

    @pytest.mark.asyncio
    async def test_docuum_command_passes_whitelist(self):
        task = ImageCleanupTask(image_whitelist=[r"^rock-base.*$", r"^pinned:.*$"])
        runtime = _runtime(_default_results())

        await task.run_action(runtime)
        docuum_cmd = runtime.execute.await_args_list[1].args[0].command
        assert "--keep '^rock-base.*$'" in docuum_cmd
        assert "--keep '^pinned:.*$'" in docuum_cmd

    @pytest.mark.asyncio
    async def test_prune_command_includes_image_and_builder_prune(self):
        task = ImageCleanupTask(keep_build_storage="10GB")
        runtime = _runtime(_default_results())

        await task.run_action(runtime)
        prune_cmd = runtime.execute.await_args_list[2].args[0].command
        assert "docker image prune -f --filter dangling=true" in prune_cmd
        assert "docker builder prune -f --keep-storage 10GB" in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_command_does_not_invoke_volume_prune(self):
        # Long-running sandboxes may attach named volumes; we don't want to
        # drop them on schedule.
        task = ImageCleanupTask()
        runtime = _runtime(_default_results())

        await task.run_action(runtime)
        prune_cmd = runtime.execute.await_args_list[2].args[0].command
        assert "docker volume prune" not in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_step_is_fail_soft(self):
        task = ImageCleanupTask()
        runtime = _runtime(_default_results())

        await task.run_action(runtime)
        prune_cmd = runtime.execute.await_args_list[2].args[0].command
        # `(...) 2>&1 || true` makes a missing/old docker subcommand non-fatal.
        assert "|| true" in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_skipped_when_keep_build_storage_falsy(self):
        task = ImageCleanupTask(keep_build_storage=None)
        # Only 2 execute calls expected: install check + docuum start.
        runtime = _runtime(_default_results()[:2])

        result = await task.run_action(runtime)
        assert runtime.execute.await_count == 2
        assert result["prune_exit_code"] is None
        assert result["prune_output_head"] == ""

    @pytest.mark.asyncio
    async def test_run_action_returns_pid_and_status(self):
        task = ImageCleanupTask()
        runtime = _runtime(_default_results(pid=98765))

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.RUNNING
        assert result["pid"] == 98765
        assert result["disk_threshold"] == "1T"
        assert result["keep_build_storage"] == "20GB"

    @pytest.mark.asyncio
    async def test_run_action_records_prune_output(self):
        task = ImageCleanupTask()
        runtime = _runtime(_default_results(prune_stdout="Total reclaimed space: 3.5GB\n(more)"))

        result = await task.run_action(runtime)
        assert "Total reclaimed space" in result["prune_output_head"]
