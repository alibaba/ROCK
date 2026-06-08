"""Tests for FileCleanupTask: -delete perf swap, blacklist guard, path validation."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.scheduler.task_base import TaskStatusEnum
from rock.admin.scheduler.tasks.file_cleanup_task import (
    _PATH_BLACKLIST,
    FileCleanupTask,
    TargetDirConfig,
)

# --------------------------------------------------------------------------- #
# Section A: TargetDirConfig.from_raw — backward-compat + path validation
# --------------------------------------------------------------------------- #


class TestTargetDirConfigFromRaw:
    def test_from_raw_with_string(self):
        cfg = TargetDirConfig.from_raw("/data/cache")
        assert cfg.path == "/data/cache"
        assert cfg.exclude_dirs == []
        assert cfg.exclude_files == []

    def test_from_raw_with_dict_full(self):
        cfg = TargetDirConfig.from_raw(
            {
                "path": "/data/cache",
                "exclude_dirs": [".git", "important"],
                "exclude_files": [".gitkeep"],
            }
        )
        assert cfg.path == "/data/cache"
        assert cfg.exclude_dirs == [".git", "important"]
        assert cfg.exclude_files == [".gitkeep"]

    def test_from_raw_with_dict_minimal(self):
        cfg = TargetDirConfig.from_raw({"path": "/data/cache"})
        assert cfg.exclude_dirs == []
        assert cfg.exclude_files == []

    @pytest.mark.parametrize("bad", [123, None, ["/data"], ()])
    def test_from_raw_rejects_unsupported_type(self, bad):
        with pytest.raises(ValueError, match="Unsupported target_dirs entry type"):
            TargetDirConfig.from_raw(bad)

    @pytest.mark.parametrize("bad_path", ["", "relative/path", "./logs"])
    def test_from_raw_rejects_relative_or_empty(self, bad_path):
        with pytest.raises(ValueError):
            TargetDirConfig.from_raw({"path": bad_path})

    def test_from_raw_rejects_dotdot(self):
        # The check must happen pre-normalize: os.path.normpath collapses
        # "/data/../etc" to "/etc" and would otherwise hide the traversal.
        with pytest.raises(ValueError, match="must not contain '..'"):
            TargetDirConfig.from_raw("/data/../etc")


# --------------------------------------------------------------------------- #
# Section B: FileCleanupTask blacklist guard (only "/" and "/tmp/miniforge")
# --------------------------------------------------------------------------- #


class TestFileCleanupTaskBlacklist:
    def test_init_accepts_safe_paths(self):
        task = FileCleanupTask(
            target_dirs=[
                TargetDirConfig(path="/data/cache"),
                TargetDirConfig(path="/data/workspace"),
            ],
        )
        assert len(task.target_dirs) == 2

    def test_init_rejects_root(self):
        with pytest.raises(ValueError, match="dangerous path"):
            FileCleanupTask(target_dirs=[TargetDirConfig(path="/")])

    def test_init_rejects_miniforge_exact(self):
        with pytest.raises(ValueError, match="dangerous path"):
            FileCleanupTask(target_dirs=[TargetDirConfig(path="/tmp/miniforge")])

    def test_init_rejects_miniforge_subtree(self):
        with pytest.raises(ValueError, match="dangerous path"):
            FileCleanupTask(target_dirs=[TargetDirConfig(path="/tmp/miniforge/python311")])

    def test_init_allows_data_logs(self):
        # /data/logs is intentionally NOT blacklisted — internal yml relies on
        # configuring it with exclude_files. This regression guards against
        # future "tighten the blacklist" PRs that would break deployments.
        task = FileCleanupTask(target_dirs=[TargetDirConfig(path="/data/logs")])
        assert task.target_dirs[0].path == "/data/logs"

    def test_init_allows_os_dirs_not_in_minimal_blacklist(self):
        # /etc, /var, /usr are intentionally NOT in our minimal blacklist.
        # "/" is exact-match only; would otherwise reject every absolute path.
        # OS-dir misconfig is a hypothetical mistake, not an observed one;
        # keeping the blacklist minimal makes future policy changes easier.
        task = FileCleanupTask(target_dirs=[TargetDirConfig(path="/etc")])
        assert task.target_dirs[0].path == "/etc"

    def test_path_blacklist_is_minimal(self):
        # Exactly two entries — keep this list short on purpose.
        assert _PATH_BLACKLIST == ("/", "/tmp/miniforge")


# --------------------------------------------------------------------------- #
# Section C: _build_cleanup_command — uses -delete (the perf change)
# --------------------------------------------------------------------------- #


class TestBuildCleanupCommand:
    def _new_task(self, **kwargs):
        return FileCleanupTask(
            target_dirs=kwargs.pop("target_dirs", [TargetDirConfig(path="/data/cache")]),
            max_age_mins=kwargs.pop("max_age_mins", 10080),
            max_file_size=kwargs.pop("max_file_size", "1G"),
            **kwargs,
        )

    def test_command_uses_delete_for_files(self):
        task = self._new_task()
        cmd = task._build_cleanup_command(TargetDirConfig(path="/data/cache"))
        assert "-delete;" in cmd
        # Old forms must be gone — guards against accidental revert.
        assert "-exec rm -f" not in cmd
        assert "-exec rm " not in cmd

    def test_command_uses_delete_for_empty_dirs(self):
        task = self._new_task()
        cmd = task._build_cleanup_command(TargetDirConfig(path="/data/cache"))
        assert "-type d -empty -delete" in cmd
        assert "-exec rmdir" not in cmd

    def test_command_includes_target_dir_existence_check(self):
        task = self._new_task()
        cmd = task._build_cleanup_command(TargetDirConfig(path="/data/cache"))
        assert 'if [ -d "/data/cache" ]; then' in cmd
        assert 'else echo "dir_not_found"; fi' in cmd
        assert 'echo "cleanup_done"' in cmd

    def test_command_includes_age_and_size_predicates(self):
        task = self._new_task(max_age_mins=4320, max_file_size="500M")
        cmd = task._build_cleanup_command(TargetDirConfig(path="/data/cache"))
        assert "-mmin +4320" in cmd
        # 500M = 500 * 1024 * 1024 = 524288000
        assert "-size +524288000c" in cmd

    def test_command_with_exclude_dirs(self):
        """Regression: -delete implies -depth which disables -prune; must use -not -path."""
        dir_cfg = TargetDirConfig(path="/data/cache", exclude_dirs=["keep_me"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        assert "-prune" not in cmd
        assert '-not -path "*/keep_me"' in cmd
        assert '-not -path "*/keep_me/*"' in cmd

    def test_command_with_exclude_files(self):
        dir_cfg = TargetDirConfig(path="/data/cache", exclude_files=["important.log"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        assert "-prune" not in cmd
        assert '-not -path "*/important.log"' in cmd

    def test_command_no_prune_anywhere(self):
        """Since both -delete (Step 1) and -depth (Step 2) disable -prune,
        no generated command should ever contain -prune."""
        dir_cfg = TargetDirConfig(
            path="/data/logs",
            exclude_dirs=["docker"],
            exclude_files=["docuum.log", "./rocklet.log"],
        )
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        assert "-prune" not in cmd

    def test_step1_exclude_dirs_plain_name(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_dirs=["docker"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        file_find = [p for p in parts if "-type f" in p][0]
        assert '-not -path "*/docker"' in file_find
        assert '-not -path "*/docker/*"' in file_find

    def test_step1_exclude_dirs_absolute_path(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_dirs=["/data/logs/special"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        file_find = [p for p in parts if "-type f" in p][0]
        assert '-not -path "/data/logs/special"' in file_find
        assert '-not -path "/data/logs/special/*"' in file_find

    def test_step1_exclude_files_relative_path(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_files=["./rocklet.log"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        file_find = [p for p in parts if "-type f" in p][0]
        assert '-not -path "/data/logs/rocklet.log"' in file_find

    def test_step2_exclude_dirs_plain_name(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_dirs=["docker"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        empty_dir_find = [p for p in parts if "-type d -empty -delete" in p][0]
        assert '-not -path "*/docker"' in empty_dir_find
        assert '-not -path "*/docker/*"' in empty_dir_find

    def test_step2_exclude_dirs_absolute_path(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_dirs=["/data/logs/special"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        empty_dir_find = [p for p in parts if "-type d -empty -delete" in p][0]
        assert '-not -path "/data/logs/special"' in empty_dir_find
        assert '-not -path "/data/logs/special/*"' in empty_dir_find

    def test_step2_exclude_dirs_relative_path(self):
        dir_cfg = TargetDirConfig(path="/data/logs", exclude_dirs=["./sub/dir"])
        task = self._new_task(target_dirs=[dir_cfg])
        cmd = task._build_cleanup_command(dir_cfg)
        parts = cmd.split(";")
        empty_dir_find = [p for p in parts if "-type d -empty -delete" in p][0]
        assert '-not -path "/data/logs/sub/dir"' in empty_dir_find
        assert '-not -path "/data/logs/sub/dir/*"' in empty_dir_find


# --------------------------------------------------------------------------- #
# Section D: from_config — yaml -> task instance, backward compat, fail-fast
# --------------------------------------------------------------------------- #


class _FakeTaskConfig:
    """Lightweight stand-in for rock.config.TaskConfig in unit tests."""

    def __init__(self, params, interval_seconds=86400):
        self.params = params
        self.interval_seconds = interval_seconds


class TestFromConfig:
    def test_from_config_legacy_string_format(self):
        task_config = _FakeTaskConfig(
            params={
                "target_dirs": ["/data/cache", "/data/scratch"],
                "max_age_mins": 1440,
                "max_file_size": "500M",
            }
        )
        task = FileCleanupTask.from_config(task_config)
        assert [dc.path for dc in task.target_dirs] == ["/data/cache", "/data/scratch"]
        assert task.max_age_mins == 1440
        assert task.max_file_size == "500M"

    def test_from_config_new_dict_format(self):
        task_config = _FakeTaskConfig(
            params={
                "target_dirs": [
                    {"path": "/data/cache", "exclude_dirs": ["keep"], "exclude_files": ["KEEP.txt"]},
                    "/data/scratch",
                ],
            }
        )
        task = FileCleanupTask.from_config(task_config)
        assert task.target_dirs[0].path == "/data/cache"
        assert task.target_dirs[0].exclude_dirs == ["keep"]
        assert task.target_dirs[1].path == "/data/scratch"

    def test_from_config_defaults_when_missing(self):
        task_config = _FakeTaskConfig(params={"target_dirs": ["/data/cache"]})
        task = FileCleanupTask.from_config(task_config)
        assert task.max_age_mins == 10080
        assert task.max_file_size == "1G"

    def test_from_config_rejects_dangerous_yaml(self):
        # Regression: yaml that contains /tmp/miniforge MUST fail at load time.
        task_config = _FakeTaskConfig(params={"target_dirs": ["/tmp/miniforge"]})
        with pytest.raises(ValueError, match="dangerous path"):
            FileCleanupTask.from_config(task_config)


# --------------------------------------------------------------------------- #
# Section E: run_action — happy path uses -delete, errors propagate
# --------------------------------------------------------------------------- #


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout="cleanup_done"):
        self.exit_code = exit_code
        self.stdout = stdout


class TestRunAction:
    @pytest.mark.asyncio
    async def test_run_action_no_target_dirs(self):
        task = FileCleanupTask(target_dirs=[])
        result = await task.run_action(runtime=AsyncMock())
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert "no target directories" in result["message"]

    @pytest.mark.asyncio
    async def test_run_action_executes_delete_command(self):
        task = FileCleanupTask(target_dirs=[TargetDirConfig(path="/data/cache")])

        runtime = AsyncMock()
        runtime._config = type("C", (), {"host": "10.0.0.1"})()
        runtime.execute = AsyncMock(return_value=_FakeExecResult())

        result = await task.run_action(runtime)
        assert result["status"] == TaskStatusEnum.SUCCESS
        assert result["target_dirs"] == ["/data/cache"]

        # Verify the executed shell command actually used -delete (not -exec rm).
        executed_cmd = runtime.execute.await_args.args[0].command
        assert "-delete" in executed_cmd
        assert "-exec rm" not in executed_cmd

    @pytest.mark.asyncio
    async def test_run_action_re_raises_on_error(self):
        task = FileCleanupTask(target_dirs=[TargetDirConfig(path="/data/cache")])

        runtime = AsyncMock()
        runtime._config = type("C", (), {"host": "10.0.0.1"})()
        runtime.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await task.run_action(runtime)
