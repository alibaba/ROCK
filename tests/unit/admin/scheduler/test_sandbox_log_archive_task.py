"""Tests for SandboxLogArchiveTask — deferred archive scheduler task (Path B).

v5 architecture: admin discovers candidate dirs + reads sentinel via
RPC, then dispatches /archive_log_dir to the worker (rocklet). All
tar/upload/rmtree happens on the worker. Admin only orchestrates.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions import ArchiveLogDirResponse
from rock.admin.scheduler.task_base import TaskStatusEnum


def _sentinel_json(days_ago: int, attempts: int = 0) -> str:
    stopped_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return json.dumps({"stopped_at": stopped_at, "attempts": attempts, "version": 1})


@pytest.fixture
def mock_rock_config():
    """Mock RockConfig.from_env() to return controlled oss config."""
    mock_cfg = MagicMock()
    mock_cfg.oss.keep_days_before_archive = 3
    mock_cfg.oss.archive_max_attempts = 3
    return mock_cfg


def _make_task(mock_rock_config, log_root="/data/sandbox_logs"):
    with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as mock_rc:
        mock_rc.from_env.return_value = mock_rock_config
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.MetricsMonitor") as mock_metrics:
            mock_metrics.create.return_value = MagicMock()
            from rock.admin.scheduler.tasks.sandbox_log_archive_task import SandboxLogArchiveTask

            return SandboxLogArchiveTask(log_root=log_root)


def _runtime_with(read_file_resp, archive_resp=None, execute_stdout=""):
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=MagicMock(stdout=execute_stdout, exit_code=0))
    runtime.read_file = AsyncMock(return_value=MagicMock(content=read_file_resp))
    runtime.archive_log_dir = AsyncMock(return_value=archive_resp) if archive_resp else AsyncMock()
    return runtime


class TestProcessOne:
    """Test _process_one outcomes — admin reads sentinel via RPC, dispatches RPC."""

    @pytest.mark.asyncio
    async def test_too_young_recent_sentinel(self, mock_rock_config):
        """Sentinel stopped_at < keep_days ago → too_young, no archive_log_dir RPC."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(read_file_resp=_sentinel_json(days_ago=1))

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/young-container",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "too_young"
        runtime.archive_log_dir.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_sentinel_missing(self, mock_rock_config):
        """read_file returns empty content → skipped_no_sentinel."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(read_file_resp="")

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/no-sentinel",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "skipped_no_sentinel"
        runtime.archive_log_dir.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_read_file_raises(self, mock_rock_config):
        """read_file raises (file missing) → skipped_no_sentinel."""
        task = _make_task(mock_rock_config)
        runtime = MagicMock()
        runtime.read_file = AsyncMock(side_effect=Exception("file not found"))
        runtime.archive_log_dir = AsyncMock()

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/missing",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "skipped_no_sentinel"
        runtime.archive_log_dir.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_on_corrupt_sentinel(self, mock_rock_config):
        """sentinel JSON unparseable → skipped_no_sentinel."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(read_file_resp="not valid json {{{")

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/corrupt",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "skipped_no_sentinel"
        runtime.archive_log_dir.assert_not_called()

    @pytest.mark.asyncio
    async def test_archived_successfully(self, mock_rock_config):
        """Sentinel ≥ keep_days + worker returns archived → propagate archived."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(
            read_file_resp=_sentinel_json(days_ago=5),
            archive_resp=ArchiveLogDirResponse(outcome="archived", attempts=0, message="ok"),
        )

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/old-container",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "archived"
        runtime.archive_log_dir.assert_awaited_once()
        request_arg = runtime.archive_log_dir.await_args.args[0]
        assert request_arg.log_dir == "/data/sandbox_logs/old-container"
        assert request_arg.container_name == "old-container"
        assert request_arg.max_attempts == 3

    @pytest.mark.asyncio
    async def test_failed_pending_propagated_from_worker(self, mock_rock_config):
        """Worker returns failed_pending (attempts < max) → admin propagates."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(
            read_file_resp=_sentinel_json(days_ago=4, attempts=0),
            archive_resp=ArchiveLogDirResponse(outcome="failed_pending", attempts=1),
        )

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/failing",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "failed_pending"

    @pytest.mark.asyncio
    async def test_failed_persist_propagated_from_worker(self, mock_rock_config):
        """Worker returns failed_persist (attempts ≥ max) → admin propagates."""
        task = _make_task(mock_rock_config)
        runtime = _runtime_with(
            read_file_resp=_sentinel_json(days_ago=10, attempts=2),
            archive_resp=ArchiveLogDirResponse(outcome="failed_persist", attempts=3),
        )

        result = await task._process_one(
            runtime=runtime,
            log_dir="/data/sandbox_logs/dead",
            keep_days=3,
            max_attempts=3,
        )
        assert result == "failed_persist"


class TestRunAction:
    """Top-level run_action: aggregates outcomes and emits metrics."""

    @pytest.mark.asyncio
    async def test_empty_log_root_returns_success(self, mock_rock_config):
        """ROCK_LOGGING_PATH empty → early return success."""
        task = _make_task(mock_rock_config, log_root=None)
        task.log_root = ""  # force empty after init
        runtime = AsyncMock()
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "no log root" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_no_candidates_returns_zero_counts(self, mock_rock_config):
        """find returns empty stdout → all counters zero."""
        task = _make_task(mock_rock_config)
        runtime = MagicMock()
        runtime.execute = AsyncMock(return_value=MagicMock(stdout="", exit_code=0))

        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["archived"] == 0
        assert result["skipped_too_young"] == 0
        assert result["failed_pending"] == 0
        assert result["failed_persist"] == 0

    @pytest.mark.asyncio
    async def test_aggregates_mixed_outcomes(self, mock_rock_config):
        """Multiple candidate dirs with mixed outcomes are tallied correctly."""
        task = _make_task(mock_rock_config)
        runtime = MagicMock()
        runtime.execute = AsyncMock(
            return_value=MagicMock(
                stdout="/data/sandbox_logs/old\n/data/sandbox_logs/young\n/data/sandbox_logs/dead\n",
                exit_code=0,
            )
        )
        runtime.read_file = AsyncMock(
            side_effect=[
                MagicMock(content=_sentinel_json(days_ago=5)),
                MagicMock(content=_sentinel_json(days_ago=1)),
                MagicMock(content=_sentinel_json(days_ago=10, attempts=2)),
            ]
        )
        runtime.archive_log_dir = AsyncMock(
            side_effect=[
                ArchiveLogDirResponse(outcome="archived", attempts=0),
                ArchiveLogDirResponse(outcome="failed_persist", attempts=3),
            ]
        )

        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["archived"] == 1
        assert result["skipped_too_young"] == 1
        assert result["failed_persist"] == 1
        assert result["failed_pending"] == 0


class TestFromConfig:
    def test_from_config_with_params(self, mock_rock_config):
        with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.admin.scheduler.tasks.sandbox_log_archive_task.MetricsMonitor") as mock_metrics:
                mock_metrics.create.return_value = MagicMock()
                from rock.admin.scheduler.tasks.sandbox_log_archive_task import SandboxLogArchiveTask

                config = MagicMock()
                config.interval_seconds = 43200
                config.params = {"log_root": "/custom/path"}
                task = SandboxLogArchiveTask.from_config(config)
                assert task.log_root == "/custom/path"
                assert task.interval_seconds == 43200
