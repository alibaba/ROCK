"""Tests for SandboxLogArchiveTask (DB-driven, no sentinel files)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.sandbox_log_archive_task import (
    SandboxLogArchiveTask,
    set_rock_config_provider,
    set_sandbox_table_provider,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeTaskConfig:
    def __init__(self, params=None, interval_seconds=86400):
        self.params = params or {}
        self.interval_seconds = interval_seconds


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout=""):
        self.exit_code = exit_code
        self.stdout = stdout


def _runtime(side_effects):
    rt = AsyncMock()
    rt._config = SimpleNamespace(host="10.0.0.1")
    rt.execute = AsyncMock(side_effect=side_effects)
    return rt


def _row(sandbox_id, state="stopped", stop_time=None):
    return {"sandbox_id": sandbox_id, "state": state, "stop_time": stop_time}


def _iso_days_ago(days, tz=timezone.utc) -> str:
    return (datetime.now(tz) - timedelta(days=days)).isoformat()


def _fake_rock_config(
    bucket="b",
    endpoint="oss-cn-hangzhou.aliyuncs.com",
    access_key_id="AKID",
    access_key_secret="AKSEC",
    keep_days=3,
    archive_prefix="rock-archives/",
):
    return SimpleNamespace(
        oss=SimpleNamespace(
            primary=SimpleNamespace(
                bucket=bucket,
                endpoint=endpoint,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
        ),
        sandbox_config=SimpleNamespace(
            log=SimpleNamespace(
                keep_days_before_archive=keep_days,
                archive_prefix=archive_prefix,
            )
        ),
    )


@pytest.fixture(autouse=True)
def reset_providers():
    yield
    set_sandbox_table_provider(None)
    set_rock_config_provider(None)


@pytest.fixture
def fake_table():
    table = MagicMock()
    table.list_by_in = AsyncMock(return_value=[])
    return table


# ---------------------------------------------------------------------------
# Init / from_config
# ---------------------------------------------------------------------------


class TestInit:
    def test_default(self):
        task = SandboxLogArchiveTask()
        assert task.type == "sandbox_log_archive"
        assert task.interval_seconds == 86400

    def test_log_root_override(self):
        task = SandboxLogArchiveTask(log_root="/custom/path/")
        assert task.log_root == "/custom/path"  # trailing slash stripped

    def test_log_root_falls_back_to_env(self, monkeypatch):
        import rock.env_vars

        monkeypatch.setattr(rock.env_vars, "ROCK_LOGGING_PATH", "/data/logs/")
        task = SandboxLogArchiveTask()
        assert task.log_root == "/data/logs"

    def test_from_config(self):
        task = SandboxLogArchiveTask.from_config(
            _FakeTaskConfig(params={"log_root": "/var/log/rock"}, interval_seconds=3600)
        )
        assert task.interval_seconds == 3600
        assert task.log_root == "/var/log/rock"


# ---------------------------------------------------------------------------
# Early-exit guards
# ---------------------------------------------------------------------------


class TestEarlyExit:
    @pytest.mark.asyncio
    async def test_skip_when_log_root_empty(self, monkeypatch, fake_table):
        import rock.env_vars

        monkeypatch.setattr(rock.env_vars, "ROCK_LOGGING_PATH", None)
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config())

        task = SandboxLogArchiveTask()
        runtime = _runtime([])
        result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "no log root" in result["message"]
        runtime.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_when_sandbox_table_unset(self, fake_table):
        set_sandbox_table_provider(None)
        set_rock_config_provider(lambda: _fake_rock_config())

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([])
        result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "sandbox_table not available" in result["message"]

    @pytest.mark.asyncio
    async def test_skip_when_rock_config_unset(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(None)

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([])
        result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "rock_config not available" in result["message"]

    @pytest.mark.asyncio
    async def test_skip_when_oss_primary_incomplete(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(bucket=""))  # bucket empty → skip

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([])
        result = await task.run_action(runtime)

        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "oss primary account not configured" in result["message"]
        runtime.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_no_candidates_returns_scanned_zero(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config())

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="")])

        result = await task.run_action(runtime)

        assert result["scanned"] == 0
        assert result["archived"] == 0
        fake_table.list_by_in.assert_not_awaited()


# ---------------------------------------------------------------------------
# Classification (orphan / alive / too_young / archived)
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.asyncio
    async def test_orphan_log_dir_skipped(self, fake_table):
        # ls returns 'sb-orphan', but DB has no row for it
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config())
        fake_table.list_by_in = AsyncMock(return_value=[])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-orphan")])

        result = await task.run_action(runtime)

        assert result["skipped_orphan"] == 1
        assert result["archived"] == 0

    @pytest.mark.asyncio
    async def test_alive_sandbox_skipped(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config())
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-alive", state="alive")])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-alive")])

        result = await task.run_action(runtime)

        assert result["skipped_alive"] == 1
        assert result["archived"] == 0

    @pytest.mark.asyncio
    async def test_stopped_too_young_skipped(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(keep_days=3))
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-young", state="stopped", stop_time=_iso_days_ago(1))])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-young")])

        result = await task.run_action(runtime)

        assert result["skipped_too_young"] == 1
        assert result["archived"] == 0

    @pytest.mark.asyncio
    async def test_stopped_old_enough_archived(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(keep_days=3))
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-old", state="stopped", stop_time=_iso_days_ago(5))])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime(
            [
                _FakeExecResult(stdout="sb-old"),  # discover
                _FakeExecResult(stdout=""),  # archive cmd
            ]
        )

        result = await task.run_action(runtime)

        assert result["archived"] == 1
        assert result["failed"] == 0
        assert runtime.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_stop_time_malformed_skipped(self, fake_table):
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config())
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-bad", state="stopped", stop_time="not-a-date")])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-bad")])

        result = await task.run_action(runtime)

        assert result["skipped_orphan"] == 1
        assert result["archived"] == 0


# ---------------------------------------------------------------------------
# Archive command details (credentials in env, key format, isolation)
# ---------------------------------------------------------------------------


class TestArchiveCommand:
    @pytest.mark.asyncio
    async def test_credentials_passed_via_env_not_argv(self, fake_table):
        """OSS_ACCESS_KEY_ID/SECRET MUST go through Command.env (so they
        don't leak into ps argv / audit logs / shell history)."""
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(access_key_id="SECRET_AK", access_key_secret="SECRET_SK"))
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-1", state="stopped", stop_time=_iso_days_ago(10))])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-1"), _FakeExecResult()])

        await task.run_action(runtime)

        archive_call = runtime.execute.await_args_list[1]
        cmd_obj = archive_call.args[0]
        assert "SECRET_AK" not in cmd_obj.command
        assert "SECRET_SK" not in cmd_obj.command
        assert cmd_obj.env["OSS_ACCESS_KEY_ID"] == "SECRET_AK"
        assert cmd_obj.env["OSS_ACCESS_KEY_SECRET"] == "SECRET_SK"

    @pytest.mark.asyncio
    async def test_archive_command_uses_build_sandbox_log_key(self, fake_table):
        """Archive key follows the format from rock/utils/archive_command.py
        — single source of truth shared with rock storage get (PR #962)."""
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(bucket="my-bucket", archive_prefix="archives/"))
        fake_table.list_by_in = AsyncMock(return_value=[_row("sb-x", state="stopped", stop_time=_iso_days_ago(5))])

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime([_FakeExecResult(stdout="sb-x"), _FakeExecResult()])

        await task.run_action(runtime)

        archive_cmd = runtime.execute.await_args_list[1].args[0].command
        # build_sandbox_log_key("sb-x", "archives/") => "archives/sandbox-logs/sb-x.tar.gz"
        assert "oss://my-bucket/archives/sandbox-logs/sb-x.tar.gz" in archive_cmd

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_loop(self, fake_table):
        """If one sandbox's archive raises, the loop continues for others."""
        set_sandbox_table_provider(lambda: fake_table)
        set_rock_config_provider(lambda: _fake_rock_config(keep_days=3))
        fake_table.list_by_in = AsyncMock(
            return_value=[
                _row("sb-fail", state="stopped", stop_time=_iso_days_ago(5)),
                _row("sb-ok", state="stopped", stop_time=_iso_days_ago(5)),
            ]
        )

        task = SandboxLogArchiveTask(log_root="/data/logs")
        runtime = _runtime(
            [
                _FakeExecResult(stdout="sb-fail\nsb-ok"),  # discover
                RuntimeError("ossutil down"),  # archive sb-fail raises
                _FakeExecResult(),  # archive sb-ok succeeds
            ]
        )

        result = await task.run_action(runtime)

        assert result["archived"] == 1
        assert result["failed"] == 1
