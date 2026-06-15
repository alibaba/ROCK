# rock/admin/scheduler/tasks/file_cleanup_task.py
import os
from dataclasses import dataclass, field

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.format import parse_size_to_bytes

logger = init_logger(name="file_cleanup", file_name=SCHEDULER_LOG_NAME)

# Path blacklist: entries that must NEVER appear in target_dirs because deleting
# them takes the whole worker (or worse, the host) down. Both entries are real
# incident sources:
#   - "/" : `find / -delete` is a full-OS wipe
#   - "/tmp/miniforge" : uv-shared Python runtime; if removed, no sandbox can start
# Other "obviously dangerous" OS dirs (/etc, /var, ...) are intentionally NOT here:
# they're hypothetical mistakes, not observed ones, and a longer list raises the
# cost of changing the policy later.
_PATH_BLACKLIST: tuple[str, ...] = (
    "/",
    "/tmp/miniforge",
)


@dataclass
class TargetDirConfig:
    """Configuration for a single target directory to clean up.

    Attributes:
        path: The directory path to clean
        exclude_dirs: Directory names or paths to exclude for this specific target dir.
            Supports plain names (-name), relative paths (-path joined with target),
            and absolute paths (-path direct match).
        exclude_files: File names or paths to exclude for this specific target dir.
            Same matching rules as exclude_dirs.
    """

    path: str
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: str | dict) -> "TargetDirConfig":
        """Create a TargetDirConfig from either a plain string or a dict.

        Supports two formats for backward compatibility:
        - str: just the directory path, no exclusions
        - dict: {"path": "/data", "exclude_dirs": [...], "exclude_files": [...]}

        Args:
            raw: A string path or a dict with path and optional exclusions

        Returns:
            A TargetDirConfig instance

        Raises:
            ValueError: path is empty, relative, or contains ".." traversal.
        """
        if isinstance(raw, str):
            instance = cls(path=raw)
        elif isinstance(raw, dict):
            instance = cls(
                path=raw["path"],
                exclude_dirs=raw.get("exclude_dirs", []),
                exclude_files=raw.get("exclude_files", []),
            )
        else:
            raise ValueError(f"Unsupported target_dirs entry type: {type(raw)}")

        cls._validate_path(instance.path)
        return instance

    @staticmethod
    def _validate_path(path: str) -> None:
        """Reject empty / non-absolute / traversal paths.

        ".." is checked against the *raw* path components — os.path.normpath
        would already collapse "/data/../etc" to "/etc", so a post-normalize
        check would silently accept the very thing we want to reject.
        """
        if not path or not isinstance(path, str):
            raise ValueError(f"target_dirs path must be a non-empty string, got: {path!r}")
        if not path.startswith("/"):
            raise ValueError(f"target_dirs path must be an absolute path, got: {path!r}")
        if ".." in path.split("/"):
            raise ValueError(f"target_dirs path must not contain '..', got: {path!r}")


class FileCleanupTask(BaseTask):
    """Scheduled task for cleaning up files based on modification time and file size,
    and removing empty directories afterwards."""

    def __init__(
        self,
        interval_seconds: int = 86400,
        target_dirs: list[TargetDirConfig] | None = None,
        max_age_mins: int = 10080,
        max_file_size: str = "1G",
    ):
        """
        Initialize file cleanup task.

        Args:
            interval_seconds: Execution interval in seconds, default 24 hours
            target_dirs: List of TargetDirConfig, each specifying a directory path
                and its own exclude_dirs/exclude_files.
            max_age_mins: Max file age in minutes since last modification, default 7 days (10080 mins).
                Also used as the minimum age guard for empty-directory deletion in Step 2,
                so a freshly-mkdir'd sandbox dir that has not yet flushed its first log
                file is not race-deleted out from under a live sandbox.
            max_file_size: Max file size threshold (e.g. "500M", "1G"), files exceeding this will be removed

        Raises:
            ValueError: a target_dirs entry hits _PATH_BLACKLIST (config-time fail-fast).
        """
        super().__init__(
            type="file_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        target_dirs = target_dirs or []
        for dc in target_dirs:
            self._assert_not_dangerous(dc.path)
        self.target_dirs = target_dirs
        self.max_age_mins = max_age_mins
        self.max_file_size = max_file_size

    @staticmethod
    def _assert_not_dangerous(path: str) -> None:
        """Reject `path` if it equals a blacklist entry or sits in its subtree.

        "/" is exact-match only: subtree match for "/" would reject every
        absolute path (since `path.startswith("/")` is true for any abs path),
        which obviously is not the intent — only `target_dirs: ["/"]` itself
        should fail.
        """
        normalized = path.rstrip("/") or "/"
        for entry in _PATH_BLACKLIST:
            in_subtree = entry != "/" and (normalized + "/").startswith(entry + "/")
            if normalized == entry or in_subtree:
                raise ValueError(
                    f"FileCleanupTask refuses dangerous path {path!r} "
                    f"(matched blacklist entry {entry!r}); "
                    f"these paths are managed by other components, "
                    f"not by file_cleanup."
                )

    @classmethod
    def from_config(cls, task_config) -> "FileCleanupTask":
        """Create task instance from config.

        Supports both legacy format (list of strings) and new format (list of dicts)
        for target_dirs. Examples:

        Legacy format:
            {"target_dirs": ["/tmp", "/var/log"]}

        New format:
            {"target_dirs": [
                {"path": "/tmp", "exclude_dirs": [".git"], "exclude_files": [".gitkeep"]},
                {"path": "/var/log", "exclude_dirs": ["important"]},
                "/data/cache"
            ]}
        """
        params = task_config.params
        raw_target_dirs = params.get("target_dirs", [])
        target_dirs = [TargetDirConfig.from_raw(entry) for entry in raw_target_dirs]
        max_age_mins = params.get("max_age_mins", 10080)
        max_file_size = params.get("max_file_size", "1G")
        return cls(
            interval_seconds=task_config.interval_seconds,
            target_dirs=target_dirs,
            max_age_mins=max_age_mins,
            max_file_size=max_file_size,
        )

    @staticmethod
    def _build_match_expr(value: str, target_dir: str) -> str:
        """Build a single find match expression based on whether the value is a name or a path.

        - Absolute path (starts with '/'): matched directly via -path.
        - Relative path (contains '/'): joined with target_dir and matched via -path.
        - Plain name (no '/'): matched via -name.

        Args:
            value: The exclusion value (name, relative path, or absolute path)
            target_dir: The target directory for resolving relative paths

        Returns:
            A find expression string like '-name "foo"' or '-path "/data/workspace/sub/dir"'
        """
        if value.startswith("/"):
            return f'-path "{value.rstrip("/")}"'
        if "/" in value:
            normalized_value = value.lstrip("./").strip("/")
            full_path = f"{target_dir.rstrip('/')}/{normalized_value}"
            return f'-path "{full_path}"'
        return f'-name "{value}"'

    @staticmethod
    def _build_not_path_pattern(value: str, target_dir: str, is_dir: bool) -> str:
        """Build a -not -path pattern for a single exclusion value.

        Args:
            value: The exclusion value (plain name, relative path, or absolute path)
            target_dir: The target directory for resolving relative paths
            is_dir: If True, also exclude descendants (*/pattern/*)

        Returns:
            A string of -not -path expressions
        """
        if value.startswith("/"):
            pattern = value.rstrip("/")
        elif "/" in value:
            normalized = value.lstrip("./").strip("/")
            pattern = f"{target_dir.rstrip('/')}/{normalized}"
        else:
            pattern = f"*/{value}"

        expr = f'-not -path "{pattern}"'
        if is_dir:
            expr += f' -not -path "{pattern}/*"'
        return expr

    @staticmethod
    def _build_exclude_expr(dir_config: "TargetDirConfig") -> str:
        """Build the find exclusion expression for directories and files.

        NOTE: -delete implies -depth (GNU find documented behavior), which
        disables -prune. We use -not -path filtering instead.

        Args:
            dir_config: The target directory configuration with its exclusions

        Returns:
            A string of -not -path exclusion expressions, or empty string if nothing to exclude.
        """
        parts = []
        target_dir = dir_config.path

        for exclude_dir in dir_config.exclude_dirs:
            parts.append(FileCleanupTask._build_not_path_pattern(exclude_dir, target_dir, is_dir=True))

        for exclude_file in dir_config.exclude_files:
            parts.append(FileCleanupTask._build_not_path_pattern(exclude_file, target_dir, is_dir=False))

        if not parts:
            return ""

        return " ".join(parts) + " "

    def _build_cleanup_command(self, dir_config: TargetDirConfig) -> str:
        """Build the shell command for cleaning up files in a single directory.

        Performance:
            ``find ... -delete`` (vs ``-exec rm -f {} +``) calls unlink(2)
            directly without forking a per-batch rm; on dirs with tens of
            thousands of files this is roughly an order of magnitude faster.

            Empty-dir cleanup uses the same idiom: ``-depth -type d -empty -delete``.

        Steps:
            1. Delete files (with exclusions) older than max_age_mins OR
               exceeding max_file_size.
            2. Remove empty directories left behind (with exclusions),
               guarded by ``-mmin +max_age_mins``.

        Why Step 2 needs the same age guard as Step 1:
            ``find -depth -type d -empty -delete`` with no age check will race
            against any sandbox that has just done ``mkdir -p`` for its log
            directory but not yet flushed its first log file. The directory is
            transiently empty for a few seconds-to-minutes; if cleanup fires in
            that window, the live sandbox's log path disappears and subsequent
            writes fail. Reusing ``max_age_mins`` here means a directory must be
            both empty AND not modified within the same retention window we
            apply to files — same threshold, same intent ("nothing recent
            lives here"), no extra knob to misconfigure.

        Args:
            dir_config: The target directory configuration with its exclusions

        Returns:
            Shell command string
        """
        target_dir = dir_config.path
        size_bytes = parse_size_to_bytes(self.max_file_size)
        size_find_expr = f"-size +{size_bytes}c"
        exclude_expr = self._build_exclude_expr(dir_config)

        dir_exclude_not_parts = []
        for exclude_dir in dir_config.exclude_dirs:
            dir_exclude_not_parts.append(self._build_not_path_pattern(exclude_dir, target_dir, is_dir=True))
        dir_exclude_expr = " ".join(dir_exclude_not_parts) + " " if dir_exclude_not_parts else ""

        # Step 1: Delete files (with exclusions) older than max_age_mins OR exceeding max_file_size
        # Step 2: Remove empty directories (bottom-up with -depth, excluding configured dirs),
        #         only if the directory itself has not been modified within max_age_mins.
        command = (
            f'if [ -d "{target_dir}" ]; then '
            f'find "{target_dir}" {exclude_expr}'
            f"-type f "
            f"\\( -mmin +{self.max_age_mins} -o {size_find_expr} \\) "
            f"-delete; "
            f'find "{target_dir}" -depth {dir_exclude_expr}'
            f"-type d -empty -mmin +{self.max_age_mins} -delete; "
            f'echo "cleanup_done"; '
            f'else echo "dir_not_found"; fi'
        )
        return command

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """Run file cleanup action on the worker.

        Iterates over all configured target directories, deletes qualifying files,
        and removes empty directories.

        Args:
            runtime: RemoteSandboxRuntime instance for the worker

        Returns:
            Result dict with cleanup details
        """
        if not self.target_dirs:
            logger.warning("No target directories configured for file cleanup task")
            return {"status": TaskStatusEnum.SUCCESS, "message": "no target directories configured"}

        results = {}
        has_error = False

        for dir_config in self.target_dirs:
            target_dir = dir_config.path
            try:
                command = self._build_cleanup_command(dir_config)
                result = await runtime.execute(
                    Command(command=command, shell=True, check=True, sandbox_id="scheduler-task")
                )
                output = result.stdout.strip() if result.stdout else ""
                results[target_dir] = {
                    "exit_code": result.exit_code,
                    "output": output,
                }
                logger.info(
                    f"[{self.type}] [{runtime._config.host}] File cleanup completed for directory '{target_dir}': {output}"
                )
            except Exception as e:
                has_error = True
                results[target_dir] = {"error": str(e)}
                logger.exception(
                    f"[{self.type}] [{runtime._config.host}] File cleanup exception for directory '{target_dir}': {e}"
                )
                raise e

        status = TaskStatusEnum.FAILED if has_error else TaskStatusEnum.SUCCESS
        return {
            "status": status,
            "target_dirs": [dc.path for dc in self.target_dirs],
            "max_age_mins": self.max_age_mins,
            "max_file_size": self.max_file_size,
            "details": results,
        }
