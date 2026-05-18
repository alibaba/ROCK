"""Tests for BuildCacheCleanupTask."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.build_cache_cleanup_task import BuildCacheCleanupTask


class _FakeTaskConfig:
    def __init__(self, params=None, interval_seconds=86400):
        self.params = params or {}
        self.interval_seconds = interval_seconds


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout="Pruned 0 entries"):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(stdout="Pruned 0 entries", exit_code=0):
    rt = AsyncMock()
    rt._config = type("C", (), {"host": "10.0.0.1"})()
    rt.execute = AsyncMock(return_value=_FakeExecResult(exit_code=exit_code, stdout=stdout))
    return rt


class TestInit:
    def test_default_tools(self):
        task = BuildCacheCleanupTask()
        assert task.type == "build_cache_cleanup"
        assert task.interval_seconds == 86400
        assert task.tools == ["uv", "pip"]

    def test_custom_tools_subset(self):
        task = BuildCacheCleanupTask(tools=["uv"])
        assert task.tools == ["uv"]

    def test_empty_tools_list_is_allowed(self):
        # Explicit empty list -> task no-ops (echo line). Different from None,
        # which means "use default".
        task = BuildCacheCleanupTask(tools=[])
        assert task.tools == []

    def test_unknown_tool_raises(self):
        with pytest.raises(ValueError, match="Unsupported build cache tool"):
            BuildCacheCleanupTask(tools=["uv", "npm"])


class TestFromConfig:
    def test_from_config_defaults(self):
        task = BuildCacheCleanupTask.from_config(_FakeTaskConfig())
        assert task.tools == ["uv", "pip"]

    def test_from_config_custom_tools(self):
        cfg = _FakeTaskConfig(params={"tools": ["pip"]}, interval_seconds=3600)
        task = BuildCacheCleanupTask.from_config(cfg)
        assert task.tools == ["pip"]
        assert task.interval_seconds == 3600


class TestRunAction:
    @pytest.mark.asyncio
    async def test_default_command_invokes_both_tools(self):
        task = BuildCacheCleanupTask()
        runtime = _runtime()

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["tools"] == ["uv", "pip"]

        cmd = runtime.execute.await_args.args[0].command
        assert "uv cache prune" in cmd
        assert "pip cache purge" in cmd
        # Each step has its own command -v guard so missing tool != failure.
        assert "command -v uv" in cmd
        assert "command -v pip" in cmd

    @pytest.mark.asyncio
    async def test_command_skips_missing_tool_with_soft_echo(self):
        # if/then/else structure emits "skipped (not installed)" only when
        # `command -v` fails — distinct from the "prune failed" branch below.
        task = BuildCacheCleanupTask()
        runtime = _runtime(stdout="No cache entries to prune\npip: skipped (not installed)")

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "skipped" in result["output_head"]

    @pytest.mark.asyncio
    async def test_command_distinguishes_prune_failure_from_missing_tool(self):
        # Regression: the old `a && b || c` form fired "skipped (not installed)"
        # even when the tool was present but prune itself errored (permission,
        # disk full, etc.). The if/then/else version must surface "prune failed"
        # in that case so operators can tell the two apart in logs.
        task = BuildCacheCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)
        cmd = runtime.execute.await_args.args[0].command

        # Both branches must be present in the generated command for both tools.
        assert "uv: prune failed" in cmd
        assert "uv: skipped (not installed)" in cmd
        assert "pip: prune failed" in cmd
        assert "pip: skipped (not installed)" in cmd
        # The mutually-exclusive shape: if/then/else replaces `a && b || c`.
        assert "if command -v uv" in cmd
        assert "if command -v pip" in cmd
        # Triple-quote bash puts `else` and `echo` on separate lines (newline as
        # statement separator); just check the keyword is present.
        assert "else" in cmd

    @pytest.mark.asyncio
    async def test_subset_of_tools(self):
        task = BuildCacheCleanupTask(tools=["uv"])
        runtime = _runtime()

        await task.run_action(runtime)
        cmd = runtime.execute.await_args.args[0].command
        assert "uv cache prune" in cmd
        assert "pip cache purge" not in cmd

    @pytest.mark.asyncio
    async def test_empty_tools_list_runs_noop(self):
        task = BuildCacheCleanupTask(tools=[])
        runtime = _runtime(stdout="no tools configured")

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        cmd = runtime.execute.await_args.args[0].command
        assert "no tools configured" in cmd
