# rock/admin/scheduler/tasks/image_cleanup_task.py
from rock import env_vars
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import PID_PREFIX, PID_SUFFIX, SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.system import extract_nohup_pid

logger = init_logger(name="image_clean", file_name=SCHEDULER_LOG_NAME)


class ImageCleanupTask(BaseTask):
    """Docker image cleanup task using docuum."""

    def __init__(
        self,
        interval_seconds: int = 3600,
        disk_threshold: str = "1T",
    ):
        """
        Initialize image cleanup task.

        Args:
            interval_seconds: Execution interval, default 1 hour
            disk_threshold: Disk threshold to trigger cleanup, default 1T
        """
        super().__init__(
            type="image_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.NON_IDEMPOTENT,
        )
        self.disk_threshold = disk_threshold

    @classmethod
    def from_config(cls, task_config) -> "ImageCleanupTask":
        """Create task instance from config."""
        disk_threshold = task_config.params.get("disk_threshold", "1T")
        return cls(
            interval_seconds=task_config.interval_seconds,
            disk_threshold=disk_threshold,
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """Run docuum image cleanup action."""
        # Check if docuum exists, install if not
        check_and_install_cmd = (
            f"command -v docuum > /dev/null 2>&1 || curl {env_vars.ROCK_DOCUUM_INSTALL_URL} -LSfs | sh"
        )
        await runtime.execute(Command(command=check_and_install_cmd, shell=True))

        docuum_dir = env_vars.ROCK_LOGGING_PATH if env_vars.ROCK_LOGGING_PATH else "/tmp"
        command = f"nohup docuum --threshold {self.disk_threshold} > {docuum_dir}/docuum.log 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX}"
        result = await runtime.execute(Command(command=command, shell=True))

        pid = extract_nohup_pid(result.stdout)

        return {"pid": pid, "disk_threshold": self.disk_threshold, "status": TaskStatusEnum.RUNNING}
