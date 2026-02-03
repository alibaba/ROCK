# rock/admin/scheduler/tasks/image_cleanup_task.py
from rock import env_vars
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import PID_PREFIX, PID_SUFFIX, SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.utils.system import extract_nohup_pid

logger = init_logger(name="image_clean", file_name=SCHEDULER_LOG_NAME)


class ImageCleanupTask(BaseTask):
    """Docker image cleanup task using docuum."""

    def __init__(
        self,
        interval_seconds: int = 3600,
        threshold: str = "1T",
    ):
        """
        Initialize image cleanup task.

        Args:
            interval_seconds: Execution interval, default 1 hour
            threshold: Disk threshold to trigger cleanup, default 1T
        """
        super().__init__(
            type="image_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.NON_IDEMPOTENT,
        )
        self.threshold = threshold

    @classmethod
    def from_config(cls, task_config) -> "ImageCleanupTask":
        """Create task instance from config."""
        threshold = task_config.params.get("threshold", "1T")
        return cls(
            interval_seconds=task_config.interval_seconds,
            threshold=threshold,
        )

    async def run_action(self, ip: str) -> dict:
        """Run docuum image cleanup action."""
        # Check if docuum exists, install if not
        check_and_install_cmd = f"command -v docuum > /dev/null 2>&1 || curl {env_vars.DOCUUM_INSTALL_URL} -LSfs | sh"
        await self.worker_client.execute(ip, check_and_install_cmd)

        docuum_dir = env_vars.ROCK_LOGGING_PATH if env_vars.ROCK_LOGGING_PATH else "/tmp"
        command = f"nohup docuum --threshold {self.threshold} > {docuum_dir}/docuum.log 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX}"
        result = await self.worker_client.execute(ip, command)

        pid = extract_nohup_pid(result.stdout)

        return {"pid": pid, "threshold": self.threshold, "status": TaskStatusEnum.RUNNING}
