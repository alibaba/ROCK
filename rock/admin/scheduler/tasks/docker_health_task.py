"""Check the Docker daemon on each worker and restart it if it has exited."""

from datetime import datetime

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="docker_health", file_name=SCHEDULER_LOG_NAME)


class DockerHealthTask(BaseTask):
    """Restart the Docker daemon on any worker where it has exited.

    Idempotent: ``docker info`` probes the daemon each cycle; the restart only
    fires when it is actually down, and ``sudo service docker start`` is a no-op
    on a worker whose daemon is already up. Records the check time + worker IP
    whenever a restart is triggered.
    """

    def __init__(self, interval_seconds: int = 60):
        super().__init__(
            type="docker_health",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        ip = runtime._config.host
        checked_at = datetime.now().isoformat()

        probe = await runtime.execute(
            Command(command="docker info", shell=True, check=False, sandbox_id="scheduler-task")
        )
        if probe.exit_code == 0:
            return {"status": TaskStatusEnum.SUCCESS, "checked_at": checked_at, "restarted": False}

        logger.info(f"[{self.type}] docker down on worker[{ip}] at {checked_at}, restarting")
        restart = await runtime.execute(
            Command(command="sudo service docker start", shell=True, check=False, sandbox_id="scheduler-task")
        )
        logger.info(f"[{self.type}] restart on worker[{ip}] exit={restart.exit_code}")
        return {
            "status": TaskStatusEnum.SUCCESS,
            "checked_at": checked_at,
            "restarted": True,
            "restart_exit_code": restart.exit_code,
        }
