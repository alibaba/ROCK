"""Tests for SandboxLogArchiveTask — deferred archival via /execute.

Architecture under test:
  - admin discovers candidate dirs via /execute (find … -name sentinel)
  - admin reads sentinel JSON via /read_file
  - admin runs `tar | ossutil cp && rm -rf` via /execute, with AK/SK in
    SandboxCommand.env so they never leak into argv
  - on failure, admin bumps attempts via /write_file (dump_state JSON)
  - exceeding archive_max_attempts → outcome `failed_persist`, dir is
    left intact for FileCleanupTask
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.metrics.constants import MetricsConstants
from rock.admin.scheduler.task_base import TaskStatusEnum


def _sentinel_json(days_ago: int, attempts: int = 0) -> str:
    stopped_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return json.dumps({"stopped_at": stopped_at, "attempts": attempts, "version": 1})


@pytest.fixture
def mock_oss_config():
    """Fully-populated primary OSS account so run_action does not early-return."""
    cfg = MagicMock()
    cfg.oss.primary.bucket = "chatos-rock"
    cfg.oss.primary.endpoint = "oss-cn-hangzhou.aliyuncs.com"
    cfg.oss.primary.access_key_id = "PRIMARY_AK"
    cfg.oss.primary.access_key_secret = "PRIMARY_SK"
    cfg.oss.bucket = ""
    cfg.oss.endpoint = ""
    cfg.oss.access_key_id = ""
    cfg.oss.access_key_secret = ""
    cfg.oss.archive_prefix = "rock-archives/"
    cfg.oss.keep_days_before_archive = 3
    cfg.oss.archive_max_attempts = 3
    return cfg


def _make_task(log_root: str = "/data/sandbox_logs"):
    with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.MetricsMonitor") as mock_metrics:
        mock_metrics.create.return_value = MagicMock()
        from rock.admin.scheduler.tasks.sandbox_log_archive_task import SandboxLogArchiveTask

        return SandboxLogArchiveTask(log_root=log_root)


def _runtime(execute_returns=None, read_file_content: str = "", write_file_ok: bool = True):
    runtime = MagicMock()
    if execute_returns is None:
        execute_returns = [MagicMock(stdout="", exit_code=0, stderr="")]
    if not isinstance(execute_returns, list):
        execute_returns = [execute_returns]
    runtime.execute = AsyncMock(side_effect=execute_returns)
    runtime.read_file = AsyncMock(return_value=MagicMock(content=read_file_content))
    runtime.write_file = AsyncMock(
        return_value=MagicMock(success=write_file_ok, message="ok" if write_file_ok else "fail")
    )
    return runtime


class TestProcessOne:
    @pytest.mark.asyncio
    async def test_too_young_skips_archive(self):
        task = _make_task()
        runtime = _runtime(read_file_content=_sentinel_json(days_ago=1))

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/young-sb",
            keep_days=3,
            max_attempts=3,
            archive_prefix="rock-archives/",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "too_young"
        runtime.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_sentinel_empty(self):
        task = _make_task()
        runtime = _runtime(read_file_content="")

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/empty",
            keep_days=3,
            max_attempts=3,
            archive_prefix="",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "skipped_no_sentinel"

    @pytest.mark.asyncio
    async def test_skipped_when_read_file_raises(self):
        task = _make_task()
        runtime = MagicMock()
        runtime.read_file = AsyncMock(side_effect=Exception("not found"))
        runtime.execute = AsyncMock()

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/missing",
            keep_days=3,
            max_attempts=3,
            archive_prefix="",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "skipped_no_sentinel"
        runtime.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_on_corrupt_sentinel(self):
        task = _make_task()
        runtime = _runtime(read_file_content="not-json {{{")

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/corrupt",
            keep_days=3,
            max_attempts=3,
            archive_prefix="",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "skipped_no_sentinel"

    @pytest.mark.asyncio
    async def test_archive_success_runs_pipeline_with_ak_in_env_only(self):
        task = _make_task()
        runtime = _runtime(
            execute_returns=[MagicMock(stdout="", exit_code=0, stderr="")],
            read_file_content=_sentinel_json(days_ago=5),
        )

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/sb-old",
            keep_days=3,
            max_attempts=3,
            archive_prefix="rock-archives/",
            bucket="chatos-rock",
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            access_key_id="LEAKY_AK",
            access_key_secret="LEAKY_SK",
        )
        assert outcome == "archived"

        # Inspect the SandboxCommand passed to /execute
        runtime.execute.assert_awaited_once()
        cmd_arg = runtime.execute.await_args.args[0]
        # AK/SK MUST be in env, not in command string. Defense against the
        # most expensive regression in this PR (credential leak via `ps`).
        assert cmd_arg.env == {"OSS_ACCESS_KEY_ID": "LEAKY_AK", "OSS_ACCESS_KEY_SECRET": "LEAKY_SK"}
        assert "LEAKY_AK" not in cmd_arg.command
        assert "LEAKY_SK" not in cmd_arg.command
        # Sanity: actually a tar|ossutil pipeline targeting the right OSS object
        assert "tar -czf -" in cmd_arg.command
        assert "ossutil cp" in cmd_arg.command
        assert "oss://chatos-rock/rock-archives/sandbox-logs/sb-old.tar.gz" in cmd_arg.command
        assert cmd_arg.shell is True

    @pytest.mark.asyncio
    async def test_failed_pending_bumps_attempts_via_write_file(self):
        task = _make_task()
        runtime = _runtime(
            execute_returns=[MagicMock(stdout="", exit_code=1, stderr="ossutil: timeout")],
            read_file_content=_sentinel_json(days_ago=4, attempts=0),
        )

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/sb-flaky",
            keep_days=3,
            max_attempts=3,
            archive_prefix="",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "failed_pending"
        runtime.write_file.assert_awaited_once()
        write_arg = runtime.write_file.await_args.args[0]
        assert write_arg.path.endswith("/.rock_stopped_at")
        # bumped attempts persisted as JSON
        body = json.loads(write_arg.content)
        assert body["attempts"] == 1
        assert body["version"] == 1

    @pytest.mark.asyncio
    async def test_failed_persist_when_attempts_reach_max(self):
        task = _make_task()
        runtime = _runtime(
            execute_returns=[MagicMock(stdout="", exit_code=1, stderr="dead")],
            read_file_content=_sentinel_json(days_ago=10, attempts=2),
        )

        outcome = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/sb-dead",
            keep_days=3,
            max_attempts=3,
            archive_prefix="",
            bucket="b",
            endpoint="e",
            access_key_id="ak",
            access_key_secret="sk",
        )
        assert outcome == "failed_persist"
        # The sentinel still gets bumped (attempts=3) so a future tick
        # doesn't endlessly retry the same dir.
        body = json.loads(runtime.write_file.await_args.args[0].content)
        assert body["attempts"] == 3


class TestRunAction:
    @pytest.mark.asyncio
    async def test_empty_log_root_returns_success(self, mock_oss_config):
        task = _make_task()
        task.log_root = ""  # force empty: env_vars fallback may otherwise populate it
        runtime = AsyncMock()
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as rc:
            rc.from_env.return_value = mock_oss_config
            result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "no log root" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_skips_when_oss_primary_incomplete(self, mock_oss_config):
        mock_oss_config.oss.primary.bucket = ""  # disables archival
        task = _make_task()
        runtime = MagicMock()
        runtime.execute = AsyncMock()  # must NOT be called when primary missing
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as rc:
            rc.from_env.return_value = mock_oss_config
            result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "oss primary" in result.get("message", "")
        runtime.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_candidates_returns_zero_counts(self, mock_oss_config):
        task = _make_task()
        runtime = _runtime(execute_returns=[MagicMock(stdout="", exit_code=0, stderr="")])
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as rc:
            rc.from_env.return_value = mock_oss_config
            result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["archived"] == 0
        assert result["skipped_too_young"] == 0
        assert result["failed_pending"] == 0
        assert result["failed_persist"] == 0

    @pytest.mark.asyncio
    async def test_aggregates_mixed_outcomes_and_emits_failed_persist_metric(self, mock_oss_config):
        task = _make_task()
        runtime = MagicMock()
        # 1) discovery `find`
        # 2) archive sb-old (success)
        # 3) archive sb-dead (fail with attempts=2 -> failed_persist)
        runtime.execute = AsyncMock(
            side_effect=[
                MagicMock(
                    stdout="/data/sandbox_logs/sb-old\n/data/sandbox_logs/sb-young\n/data/sandbox_logs/sb-dead\n",
                    exit_code=0,
                    stderr="",
                ),
                MagicMock(stdout="", exit_code=0, stderr=""),
                MagicMock(stdout="", exit_code=1, stderr="ossutil fail"),
            ]
        )
        runtime.read_file = AsyncMock(
            side_effect=[
                MagicMock(content=_sentinel_json(days_ago=5)),
                MagicMock(content=_sentinel_json(days_ago=1)),
                MagicMock(content=_sentinel_json(days_ago=10, attempts=2)),
            ]
        )
        runtime.write_file = AsyncMock(return_value=MagicMock(success=True, message=""))

        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as rc:
            rc.from_env.return_value = mock_oss_config
            result = await task.run_action(runtime)

        assert result["archived"] == 1
        assert result["skipped_too_young"] == 1
        assert result["failed_persist"] == 1
        assert result["failed_pending"] == 0

        # Metric: only failed_persist increments, with a sandbox_id label
        task.metrics_monitor.record_counter_by_name.assert_called_once()
        call = task.metrics_monitor.record_counter_by_name.call_args
        assert call.args[0] == MetricsConstants.SANDBOX_LOG_ARCHIVE_FAILED_PERSIST
        assert call.args[1] == 1
        assert call.args[2] == {"sandbox_id": "sb-dead"}


class TestFromConfig:
    def test_from_config_with_params(self):
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.MetricsMonitor") as mock_metrics:
            mock_metrics.create.return_value = MagicMock()
            from rock.admin.scheduler.tasks.sandbox_log_archive_task import SandboxLogArchiveTask

            cfg = MagicMock()
            cfg.interval_seconds = 43200
            cfg.params = {"log_root": "/custom/path"}
            task = SandboxLogArchiveTask.from_config(cfg)
            assert task.log_root == "/custom/path"
            assert task.interval_seconds == 43200
