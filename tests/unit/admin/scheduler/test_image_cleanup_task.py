"""Tests for ImageCleanupTask (docuum LRU + dangling/BuildKit prune).

Architecture (post split):
- ``run_action`` only launches docuum daemon (NON-idempotent).
- ``_run_prune`` performs dangling/BuildKit prune (IDEMPOTENT).
- ``run_on_worker`` overrides base: prune unconditionally, then gate
  docuum on ``should_run``. This decoupling fixes the regression where
  the whole task was skipped once docuum was alive, causing dangling
  layers and BuildKit cache to accumulate forever.
"""

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


# Call sequence for _launch_docuum (run_action):
#   1) docuum install check
#   2) docuum start (returns PID-tagged stdout)
def _docuum_results(pid=12345):
    return [
        _FakeExecResult(),
        _FakeExecResult(stdout=f"PIDSTART{pid}PIDEND"),
    ]


# Call sequence for _run_prune (single combined cmd):
#   1) docker image prune + docker builder prune (one combined cmd)
def _prune_results(prune_stdout="Total reclaimed space: 1.2GB"):
    return [_FakeExecResult(stdout=prune_stdout)]


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
    """run_action now only launches docuum — prune moved to _run_prune."""

    @pytest.mark.asyncio
    async def test_docuum_command_includes_threshold(self):
        task = ImageCleanupTask(disk_threshold="500G")
        runtime = _runtime(_docuum_results())

        await task.run_action(runtime)

        # call 0: install check; call 1: docuum start
        docuum_cmd = runtime.execute.await_args_list[1].args[0].command
        assert "docuum --threshold 500G" in docuum_cmd

    @pytest.mark.asyncio
    async def test_docuum_command_passes_whitelist(self):
        task = ImageCleanupTask(image_whitelist=[r"^rock-base.*$", r"^pinned:.*$"])
        runtime = _runtime(_docuum_results())

        await task.run_action(runtime)

        docuum_cmd = runtime.execute.await_args_list[1].args[0].command
        assert "--keep '^rock-base.*$'" in docuum_cmd
        assert "--keep '^pinned:.*$'" in docuum_cmd

    @pytest.mark.asyncio
    async def test_run_action_returns_pid_and_status(self):
        task = ImageCleanupTask()
        runtime = _runtime(_docuum_results(pid=98765))

        result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.RUNNING
        assert result["pid"] == 98765
        assert result["disk_threshold"] == "1T"
        assert result["image_whitelist"] == []

    @pytest.mark.asyncio
    async def test_run_action_does_not_invoke_prune(self):
        """Regression guard: run_action MUST NOT call prune (decoupled to _run_prune)."""
        task = ImageCleanupTask()
        runtime = _runtime(_docuum_results())

        await task.run_action(runtime)

        # exactly 2 execute calls: install check + docuum start (NO prune call)
        assert runtime.execute.await_count == 2
        for call in runtime.execute.await_args_list:
            cmd = call.args[0].command
            assert "docker image prune" not in cmd
            assert "docker builder prune" not in cmd


class TestRunPrune:
    """_run_prune is idempotent and runs every cycle (via run_on_worker)."""

    @pytest.mark.asyncio
    async def test_prune_command_includes_image_and_builder_prune(self):
        task = ImageCleanupTask(keep_build_storage="10GB")
        runtime = _runtime(_prune_results())

        await task._run_prune(runtime)

        prune_cmd = runtime.execute.await_args_list[0].args[0].command
        assert "docker image prune -f --filter dangling=true" in prune_cmd
        assert "docker builder prune -f --keep-storage 10GB" in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_command_does_not_invoke_volume_prune(self):
        # Long-running sandboxes may attach named volumes; we don't want to
        # drop them on schedule.
        task = ImageCleanupTask()
        runtime = _runtime(_prune_results())

        await task._run_prune(runtime)

        prune_cmd = runtime.execute.await_args_list[0].args[0].command
        assert "docker volume prune" not in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_step_is_fail_soft(self):
        task = ImageCleanupTask()
        runtime = _runtime(_prune_results())

        await task._run_prune(runtime)

        prune_cmd = runtime.execute.await_args_list[0].args[0].command
        # `(...) 2>&1 || true` makes a missing/old docker subcommand non-fatal.
        assert "|| true" in prune_cmd

    @pytest.mark.asyncio
    async def test_prune_records_output(self):
        task = ImageCleanupTask()
        runtime = _runtime(_prune_results(prune_stdout="Total reclaimed space: 3.5GB\n(more)"))

        result = await task._run_prune(runtime)

        assert "Total reclaimed space" in result["prune_output_head"]
        assert result["prune_exit_code"] == 0
        assert result["keep_build_storage"] == "20GB"

    @pytest.mark.asyncio
    async def test_prune_skipped_when_keep_build_storage_falsy(self):
        """keep_build_storage=None → fast return, no docker exec."""
        task = ImageCleanupTask(keep_build_storage=None)
        runtime = AsyncMock()
        runtime.execute = AsyncMock()

        result = await task._run_prune(runtime)

        assert result == {"prune_exit_code": None, "prune_output_head": ""}
        runtime.execute.assert_not_awaited()


class TestRunOnWorker:
    """run_on_worker overrides base: prune unconditionally, then gate docuum on should_run.

    Verifies the fix for the regression where the entire task was skipped once
    docuum daemon was alive (NON_IDEMPOTENT idempotency type → should_run False
    → run_action skipped → prune never ran → dangling layers accumulated forever).
    """

    @pytest.mark.asyncio
    async def test_first_run_launches_both(self, monkeypatch):
        """status=None / docuum dead → prune runs AND single_run (docuum launch) runs."""
        task = ImageCleanupTask()
        runtime = AsyncMock()
        monkeypatch.setattr(task, "_get_runtime", lambda ip: runtime)
        monkeypatch.setattr(task, "should_run", AsyncMock(return_value=True))
        monkeypatch.setattr(task, "single_run", AsyncMock(return_value={"status": TaskStatusEnum.RUNNING, "pid": 1}))
        prune_spy = AsyncMock(return_value={"prune_exit_code": 0})
        monkeypatch.setattr(task, "_run_prune", prune_spy)

        result = await task.run_on_worker("10.0.0.1")

        prune_spy.assert_awaited_once_with(runtime)
        task.single_run.assert_awaited_once_with(runtime, "10.0.0.1")
        assert result == {"status": TaskStatusEnum.RUNNING, "pid": 1, "prune_exit_code": 0}

    @pytest.mark.asyncio
    async def test_docuum_alive_still_prunes(self, monkeypatch):
        """CORE REGRESSION: docuum pid alive (should_run=False) → prune STILL runs.

        Before the fix, base run_on_worker would skip the entire task when
        should_run returned False, blocking prune indefinitely once docuum
        was alive (which is forever — docuum is a long-running daemon).
        """
        task = ImageCleanupTask()
        runtime = AsyncMock()
        monkeypatch.setattr(task, "_get_runtime", lambda ip: runtime)
        monkeypatch.setattr(task, "should_run", AsyncMock(return_value=False))
        single_run = AsyncMock(return_value={"status": TaskStatusEnum.RUNNING})
        monkeypatch.setattr(task, "single_run", single_run)
        prune_spy = AsyncMock(return_value={"prune_exit_code": 0})
        monkeypatch.setattr(task, "_run_prune", prune_spy)

        result = await task.run_on_worker("10.0.0.1")

        prune_spy.assert_awaited_once_with(runtime)
        single_run.assert_not_awaited()  # docuum NOT relaunched (correctly)
        assert result == {
            "status": TaskStatusEnum.SUCCESS,
            "action": "prune_only",
            "prune_exit_code": 0,
        }

    @pytest.mark.asyncio
    async def test_prune_exception_does_not_block_docuum(self, monkeypatch):
        """prune raises → docuum launch still proceeds per should_run; no crash propagates."""
        task = ImageCleanupTask()
        runtime = AsyncMock()
        monkeypatch.setattr(task, "_get_runtime", lambda ip: runtime)
        monkeypatch.setattr(task, "_run_prune", AsyncMock(side_effect=RuntimeError("docker down")))
        monkeypatch.setattr(task, "should_run", AsyncMock(return_value=True))
        single_run = AsyncMock(return_value={"status": TaskStatusEnum.RUNNING})
        monkeypatch.setattr(task, "single_run", single_run)

        # Must not raise (run_on_worker catches prune exception with try/except)
        await task.run_on_worker("10.0.0.1")

        single_run.assert_awaited_once_with(runtime, "10.0.0.1")

    @pytest.mark.asyncio
    async def test_docuum_dead_relaunches(self, monkeypatch):
        """docuum pid not alive (should_run=True) → single_run called → docuum relaunched."""
        task = ImageCleanupTask()
        runtime = AsyncMock()
        monkeypatch.setattr(task, "_get_runtime", lambda ip: runtime)
        monkeypatch.setattr(task, "should_run", AsyncMock(return_value=True))
        single_run = AsyncMock(return_value={"status": TaskStatusEnum.RUNNING})
        monkeypatch.setattr(task, "single_run", single_run)
        monkeypatch.setattr(task, "_run_prune", AsyncMock(return_value={}))

        await task.run_on_worker("10.0.0.1")

        single_run.assert_awaited_once_with(runtime, "10.0.0.1")

    @pytest.mark.asyncio
    async def test_prune_runs_before_docuum_check(self, monkeypatch):
        """Order matters: prune must complete (or fail-soft) BEFORE should_run check.

        Guarantees prune always gets its chance even if should_run later
        decides to skip docuum launch.
        """
        task = ImageCleanupTask()
        runtime = AsyncMock()
        call_order = []
        monkeypatch.setattr(task, "_get_runtime", lambda ip: runtime)

        async def fake_prune(rt):
            call_order.append("prune")
            return {}

        async def fake_should_run(rt):
            call_order.append("should_run")
            return False

        monkeypatch.setattr(task, "_run_prune", fake_prune)
        monkeypatch.setattr(task, "should_run", fake_should_run)
        monkeypatch.setattr(task, "single_run", AsyncMock())

        await task.run_on_worker("10.0.0.1")

        assert call_order == ["prune", "should_run"]
