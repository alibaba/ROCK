# rock/admin/scheduler/tasks/container_cleanup_task.py
from rock import env_vars
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import PID_PREFIX, PID_SUFFIX, SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.system import extract_nohup_pid

logger = init_logger(name="container_cleanup", file_name=SCHEDULER_LOG_NAME)


class ContainerCleanupTask(BaseTask):
    """Scheduled task for cleaning up stopped Docker containers older than a specified age."""

    def __init__(
        self,
        interval_seconds: int = 86400,
        max_age_hours: int = 24,
    ):
        """
        Initialize container cleanup task.

        Args:
            interval_seconds: Execution interval in seconds, default 24 hours (86400s)
            max_age_hours: Max container age in hours since it stopped, default 24 hours
        """
        super().__init__(
            type="container_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.max_age_hours = max_age_hours

    @classmethod
    def from_config(cls, task_config) -> "ContainerCleanupTask":
        """Create task instance from config."""
        max_age_hours = task_config.params.get("max_age_hours", 24)
        return cls(
            interval_seconds=task_config.interval_seconds,
            max_age_hours=max_age_hours,
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """Run container cleanup action.

        Removes exited Docker containers whose finish time exceeds max_age_hours.
        """
        log_dir = env_vars.ROCK_LOGGING_PATH if env_vars.ROCK_LOGGING_PATH else "/tmp"
        command = (
            f"nohup bash -c '"
            f"docker ps -aq --filter status=created | xargs -r docker rm; "
            f'cutoff=$(date -d "{self.max_age_hours} hours ago" +%s); '
            f"docker ps -aq --filter status=exited | "
            f'xargs -r docker inspect --format "{{{{.Id}}}} {{{{.State.FinishedAt}}}}" | '
            f"while read -r id finished; do "
            f'[ "$finished" = "0001-01-01T00:00:00Z" ] && continue; '
            f'finished_ts=$(date -d "$finished" +%s 2>/dev/null) || continue; '
            f'[ "$finished_ts" -lt "$cutoff" ] && docker rm "$id"; '
            f"done"
            f"' > {log_dir}/container_cleanup.log 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX}"
        )

        result = await runtime.execute(Command(command=command, shell=True))

        pid = extract_nohup_pid(result.stdout)
        logger.info(
            f"container cleanup task [{pid}] run successfully on worker[{runtime._config.host}], "
            f"max_age_hours={self.max_age_hours}"
        )

        return {
            "pid": pid,
            "max_age_hours": self.max_age_hours,
            "status": TaskStatusEnum.RUNNING,
        }
