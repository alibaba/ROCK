# rock/admin/scheduler/tasks/file_cleanup_task.py
from dataclasses import dataclass, field

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.format import parse_size_to_bytes

logger = init_logger(name="file_cleanup", file_name=SCHEDULER_LOG_NAME)


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
        """
        if isinstance(raw, str):
            return cls(path=raw)
        if isinstance(raw, dict):
            return cls(
                path=raw["path"],
                exclude_dirs=raw.get("exclude_dirs", []),
                exclude_files=raw.get("exclude_files", []),
            )
        raise ValueError(f"Unsupported target_dirs entry type: {type(raw)}")


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
            max_age_mins: Max file age in minutes since last modification, default 7 days (10080 mins)
            max_file_size: Max file size threshold (e.g. "500M", "1G"), files exceeding this will be removed
        """
        super().__init__(
            type="file_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.target_dirs = target_dirs or []
        self.max_age_mins = max_age_mins
        self.max_file_size = max_file_size

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
    def _build_exclude_expr(dir_config: "TargetDirConfig") -> str:
        """Build the find exclusion expression for directories and files.

        Supports both name-based and path-based exclusions. Values containing '/'
        are treated as relative paths under target_dir and matched via -path.
        Plain names are matched via -name.

        Args:
            dir_config: The target directory configuration with its exclusions

        Returns:
            A string of find prune/exclude expressions, or empty string if nothing to exclude.
        """
        parts = []
        target_dir = dir_config.path

        for exclude_dir in dir_config.exclude_dirs:
            match_expr = FileCleanupTask._build_match_expr(exclude_dir, target_dir)
            parts.append(f"{match_expr} -prune")

        for exclude_file in dir_config.exclude_files:
            match_expr = FileCleanupTask._build_match_expr(exclude_file, target_dir)
            parts.append(f"{match_expr} -prune")

        if not parts:
            return ""

        return "\\( " + " -o ".join(parts) + " \\) -o "

    def _build_cleanup_command(self, dir_config: TargetDirConfig) -> str:
        """Build the shell command for cleaning up files in a single directory.

        The command performs two steps:
        1. Delete files matching either condition: older than max_age_mins OR larger than max_file_size
           (excluding configured directories and files)
        2. Remove empty directories left behind (excluding configured directories)

        Args:
            dir_config: The target directory configuration with its exclusions

        Returns:
            Shell command string
        """
        target_dir = dir_config.path
        size_bytes = parse_size_to_bytes(self.max_file_size)
        size_find_expr = f"-size +{size_bytes}c"
        exclude_expr = self._build_exclude_expr(dir_config)

        # Build directory exclusion for empty dir cleanup
        dir_exclude_parts = []
        for exclude_dir in dir_config.exclude_dirs:
            match_expr = self._build_match_expr(exclude_dir, target_dir)
            dir_exclude_parts.append(match_expr)
        dir_exclude_expr = ""
        if dir_exclude_parts:
            dir_exclude_expr = "\\( " + " -o ".join(dir_exclude_parts) + " \\) -prune -o "

        # Step 1: Delete files (with exclusions) older than max_age_mins OR exceeding max_file_size
        # Step 2: Remove empty directories (bottom-up with -depth, excluding configured dirs)
        command = (
            f'if [ -d "{target_dir}" ]; then '
            f'find "{target_dir}" {exclude_expr}'
            f"-type f "
            f"\\( -mmin +{self.max_age_mins} -o {size_find_expr} \\) "
            f"-exec rm -f {{}} +; "
            f'find "{target_dir}" -depth {dir_exclude_expr}'
            f"-type d -empty -exec rmdir {{}} +; "
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
                result = await runtime.execute(Command(command=command, shell=True, check=True))
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
