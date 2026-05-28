"""Tests for RayLogCleanupTask (3-stage: stale sessions + logs/ PID-aware + logs/old/)."""

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


# ---------------------------------------------------------------------------
# Init / from_config
# ---------------------------------------------------------------------------


class TestInit:
    def test_default(self):
        task = RayLogCleanupTask()
        assert task.type == "ray_log_cleanup"
        assert task.ray_temp_dir == "/data/tmp/ray"
        assert task.min_age_hours == 24
        assert task.live_log_keep_days == 7
        assert task.old_logs_keep_hours == 24
        assert task.setup_log_keep_minutes == 60

    def test_strips_trailing_slash(self):
        task = RayLogCleanupTask(ray_temp_dir="/data/ray/")
        assert task.ray_temp_dir == "/data/ray"

    def test_rejects_min_age_below_one(self):
        with pytest.raises(ValueError, match="min_age_hours must be >= 1"):
            RayLogCleanupTask(min_age_hours=0)

    def test_rejects_live_log_keep_days_below_one(self):
        with pytest.raises(ValueError, match="live_log_keep_days must be >= 1"):
            RayLogCleanupTask(live_log_keep_days=0)

    def test_rejects_old_logs_keep_hours_below_one(self):
        with pytest.raises(ValueError, match="old_logs_keep_hours must be >= 1"):
            RayLogCleanupTask(old_logs_keep_hours=0)

    def test_rejects_setup_log_keep_minutes_below_one(self):
        with pytest.raises(ValueError, match="setup_log_keep_minutes must be >= 1"):
            RayLogCleanupTask(setup_log_keep_minutes=0)

    def test_custom_thresholds(self):
        task = RayLogCleanupTask(live_log_keep_days=3, old_logs_keep_hours=12)
        assert task.live_log_keep_days == 3
        assert task.old_logs_keep_hours == 12


class TestFromConfig:
    def test_from_config_defaults(self):
        task = RayLogCleanupTask.from_config(_FakeTaskConfig())
        assert task.ray_temp_dir == "/data/tmp/ray"
        assert task.min_age_hours == 24
        assert task.live_log_keep_days == 7
        assert task.old_logs_keep_hours == 24
        assert task.setup_log_keep_minutes == 60

    def test_from_config_custom(self):
        cfg = _FakeTaskConfig(
            params={
                "ray_temp_dir": "/data/ray",
                "min_age_hours": 48,
                "live_log_keep_days": 14,
                "old_logs_keep_hours": 6,
                "setup_log_keep_minutes": 30,
            },
            interval_seconds=3600,
        )
        task = RayLogCleanupTask.from_config(cfg)
        assert task.ray_temp_dir == "/data/ray"
        assert task.min_age_hours == 48
        assert task.live_log_keep_days == 14
        assert task.old_logs_keep_hours == 6
        assert task.setup_log_keep_minutes == 30
        assert task.interval_seconds == 3600


# ---------------------------------------------------------------------------
# Shell command shape — verify the 3 stages and their parameters
# ---------------------------------------------------------------------------


class TestCommandShape:
    @pytest.mark.asyncio
    async def test_part1_skips_session_latest(self):
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
    async def test_part1_uses_min_age_in_minutes(self):
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
        assert "/data/ray/session_latest/logs" in cmd

    @pytest.mark.asyncio
    async def test_part2a_uses_kill_zero_pid_probe(self):
        """PART 2a must probe PID with `kill -0` (no-signal liveness check)."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        assert "kill -0" in cmd
        # PID regex must match Ray's worker file naming
        assert "[_-][0-9]+" in cmd
        # Only files older than 60 min are candidates (race-window guard)
        assert "-mmin +60" in cmd

    @pytest.mark.asyncio
    async def test_part2b_uses_live_log_keep_days(self):
        """PART 2b stale-file mtime threshold = live_log_keep_days * 24 * 60 minutes."""
        task = RayLogCleanupTask(live_log_keep_days=7)
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # 7d * 24h * 60min = 10080
        assert "-mmin +10080" in cmd

    @pytest.mark.asyncio
    async def test_part2b_daemon_whitelist_protected(self):
        """raylet*, gcs_server*, runtime_env_agent*, dashboard*, monitor*,
        log_monitor*, agent-* must all be excluded by name (regression guard
        — Ray holds fds; deletion wouldn't free disk)."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        for daemon in ("raylet", "gcs_server", "runtime_env_agent", "dashboard", "monitor", "log_monitor"):
            assert f"! -name '{daemon}*'" in cmd, f"daemon whitelist missing: {daemon}"
        assert "! -name 'agent-*'" in cmd, "daemon whitelist missing: agent-*"

    @pytest.mark.asyncio
    async def test_part2a_daemon_whitelist_protects_agent_files(self):
        """CORE REGRESSION (task #25): PART 2a (PID-aware) MUST also exclude
        daemon-named files BEFORE the kill -0 probe. Without this guard,
        `agent-<id>.err` matches the PID regex; kill -0 <id> fails because
        <id> is a Ray-generated agent identifier (NOT a Linux PID) that
        exceeds the PID range, so the file gets wrongly removed even though
        Ray's runtime env agent is still writing to it."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # PART 2a section starts with `-mmin +60` (race-window guard).
        # Find the segment from "+60" up to the next pipe (end of find expr).
        idx = cmd.find("-mmin +60")
        assert idx >= 0, "PART 2a -mmin +60 marker not found"
        part2a = cmd[idx : cmd.find("| while read -r f", idx)]
        # All daemon whitelist names must appear in PART 2a's find filter,
        # not just PART 2b. Otherwise agent-* / runtime_env_agent* etc. get
        # wrongly probed and removed.
        for daemon in (
            "raylet",
            "gcs_server",
            "runtime_env_agent",
            "dashboard",
            "monitor",
            "log_monitor",
            "agent-",
        ):
            assert f"! -name '{daemon}*'" in part2a, f"PART 2a missing daemon whitelist: {daemon}"

    @pytest.mark.asyncio
    async def test_part2c_uses_setup_log_keep_minutes(self):
        """PART 2c (runtime_env_setup-*) mtime threshold = setup_log_keep_minutes."""
        task = RayLogCleanupTask(setup_log_keep_minutes=30)
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # The first textual occurrence is the comment block; search for the
        # actual filter line "-name 'runtime_env_setup-*'" to skip past it.
        idx = cmd.find("-name 'runtime_env_setup-*'")
        assert idx >= 0, "PART 2c -name 'runtime_env_setup-*' filter not found"
        window = cmd[max(0, idx - 150):idx]
        assert "-mmin +30" in window, f"PART 2c missing -mmin +30 in nearby window: {window!r}"

    @pytest.mark.asyncio
    async def test_part2c_targets_runtime_env_setup_glob(self):
        """PART 2c must use -name 'runtime_env_setup-*' (covers both digit and hex suffix)."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        assert "-name 'runtime_env_setup-*'" in cmd
        # Emits removed_setup= marker so output parser can count it
        assert "removed_setup=" in cmd

    @pytest.mark.asyncio
    async def test_part3_uses_old_logs_keep_hours(self):
        """PART 3 old-dir mtime threshold = old_logs_keep_hours * 60 minutes."""
        task = RayLogCleanupTask(old_logs_keep_hours=24)
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # 24h * 60 = 1440
        assert "-mmin +1440" in cmd
        assert "session_latest/logs/old" in cmd

    @pytest.mark.asyncio
    async def test_part3_uses_old_logs_keep_hours_custom(self):
        task = RayLogCleanupTask(old_logs_keep_hours=6)
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # 6h * 60 = 360
        assert "-mmin +360" in cmd


# ---------------------------------------------------------------------------
# Output parsing — per-category counters
# ---------------------------------------------------------------------------


class TestOutputParsing:
    @pytest.mark.asyncio
    async def test_extracts_part1_removed_sessions(self):
        """PART 1 session removals reported via `removed=<sess>` (backward compat)."""
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
    async def test_extracts_part2a_dead_pid_count(self):
        stdout = (
            "live_session=session_xxx\n"
            "removed_dead_pid_log=python-core-worker-aaaa_12345.log\n"
            "removed_dead_pid_log=worker-bbbb-c205-67890.err\n"
            "removed_dead_pid_log=worker-bbbb-c205-67890.out\n"
            "ray_log_cleanup_done"
        )
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["removed_dead_pid_count"] == 3

    @pytest.mark.asyncio
    async def test_extracts_part2b_stale_count(self):
        stdout = (
            "live_session=session_xxx\n"
            "removed_stale_file=runtime_env_setup-60010000.log\n"
            "removed_stale_file=runtime_env_setup-5c010000.log\n"
            "ray_log_cleanup_done"
        )
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["removed_stale_count"] == 2

    @pytest.mark.asyncio
    async def test_extracts_part2c_setup_count_digit_and_hex(self):
        """REGRESSION: PART 2c must count both digit-suffix AND hex-suffix
        runtime_env_setup files. Pre-fix, only the digit variant was cleaned
        (accidentally by PART 2a's pid_max overflow); hex variant waited 7d
        for PART 2b."""
        stdout = (
            "live_session=session_xxx\n"
            "removed_setup=runtime_env_setup-31050000.log\n"   # pure digits
            "removed_setup=runtime_env_setup-f4060000.log\n"   # hex — the main fix
            "removed_setup=runtime_env_setup-b6000000.log\n"   # hex
            "ray_log_cleanup_done"
        )
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["removed_setup_count"] == 3

    @pytest.mark.asyncio
    async def test_extracts_part3_old_count(self):
        stdout = (
            "live_session=session_xxx\n"
            "removed_old=python-core-worker-aaa.log.1\n"
            "removed_old=python-core-worker-aaa.log.2\n"
            "removed_old=raylet.out.1\n"
            "ray_log_cleanup_done"
        )
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["removed_old_count"] == 3

    @pytest.mark.asyncio
    async def test_all_counters_zero_when_nothing_removed(self):
        stdout = "live_session=session_xxx\nray_log_cleanup_done"
        task = RayLogCleanupTask()
        runtime = _runtime(stdout=stdout)

        result = await task.run_action(runtime)
        assert result["removed_count"] == 0
        assert result["removed_dead_pid_count"] == 0
        assert result["removed_setup_count"] == 0
        assert result["removed_stale_count"] == 0
        assert result["removed_old_count"] == 0

    @pytest.mark.asyncio
    async def test_handles_missing_ray_dir(self):
        task = RayLogCleanupTask()
        runtime = _runtime(stdout="ray_temp_dir_not_found")

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["removed_count"] == 0
        assert result["removed_dead_pid_count"] == 0
        assert result["removed_stale_count"] == 0
        assert result["removed_old_count"] == 0


# ---------------------------------------------------------------------------
# Regression guards — must-have command properties
# ---------------------------------------------------------------------------


class TestRegressionGuards:
    @pytest.mark.asyncio
    async def test_command_does_not_recurse_into_session_dirs_at_top_level(self):
        """Top-level walk MUST keep `-maxdepth 1`; we explicitly DO recurse
        into `session_latest/logs/old/` in PART 3 but never into other
        `session_<ts>_<pid>` internals (only whole-dir rm -rf for those)."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        # PART 1 stale-session walk: must have maxdepth 1
        assert '-maxdepth 1 -type d -name "session_*"' in cmd

    @pytest.mark.asyncio
    async def test_command_uses_session_latest_logs_path(self):
        """PART 2/3 must scope to session_latest/logs explicitly, not arbitrary session dirs."""
        task = RayLogCleanupTask()
        runtime = _runtime()
        await task.run_action(runtime)

        cmd = runtime.execute.await_args.args[0].command
        assert "session_latest/logs" in cmd
