"""Drop stale /data/tmp/ray/session_* dirs on each worker."""

import textwrap

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="ray_log_cleanup", file_name=SCHEDULER_LOG_NAME)


class RayLogCleanupTask(BaseTask):
    """Drop /data/tmp/ray/session_* dirs that are NOT the live session.

    Ray restarts (cluster up/down, head failover) leave dozens of
    session_<timestamp>_<pid> dirs behind. The currently active one is
    symlinked as `session_latest`; we resolve that link and skip its target.
    Sessions younger than `min_age_hours` are also kept as a buffer against
    a stale symlink.

    NOTE: This is the WORKER side. The ray-head's /data/tmp/ray is cleaned
    by a daily cron baked into the head Dockerfile (rock-internal repo);
    rocklet is not deployed on the head and the worker scheduler does not
    reach it.
    """

    def __init__(
        self,
        interval_seconds: int = 86400,
        ray_temp_dir: str = "/data/tmp/ray",
        min_age_hours: int = 24,
    ):
        """
        Args:
            interval_seconds: Execution interval, default 24 hours.
            ray_temp_dir: Ray's --temp-dir, default /data/tmp/ray.
            min_age_hours: Only delete session dirs whose mtime is older than
                this AND that are not session_latest. Default 24h.
        """
        super().__init__(
            type="ray_log_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        if min_age_hours < 1:
            raise ValueError(f"ray_log_cleanup.min_age_hours must be >= 1, got {min_age_hours}")
        self.ray_temp_dir = ray_temp_dir.rstrip("/")
        self.min_age_hours = min_age_hours

    @classmethod
    def from_config(cls, task_config) -> "RayLogCleanupTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            ray_temp_dir=task_config.params.get("ray_temp_dir", "/data/tmp/ray"),
            min_age_hours=task_config.params.get("min_age_hours", 24),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        ray_dir = self.ray_temp_dir
        max_age_min = self.min_age_hours * 60
        # Resolve session_latest -> live basename, then list session_* dirs
        # older than threshold and rm -rf those that are not the live one.
        # textwrap.dedent strips common leading whitespace so source can be
        # indented for readability without polluting the emitted shell.
        command = textwrap.dedent(
            f"""\
            if [ -d "{ray_dir}" ]; then
              LIVE=$(readlink "{ray_dir}/session_latest" 2>/dev/null | xargs -I{{}} basename {{}} 2>/dev/null)
              echo "live_session=${{LIVE:-<none>}}"
              find "{ray_dir}" -maxdepth 1 -type d -name "session_*" \\
                ! -name "session_latest" -mmin +{max_age_min} \\
              | while read -r d; do
                  bn=$(basename "$d")
                  if [ "$bn" != "$LIVE" ]; then
                    rm -rf "$d" && echo "removed=$bn"
                  fi
                done
              echo "ray_log_cleanup_done"
            else
              echo "ray_temp_dir_not_found"
            fi"""
        )
        result = await runtime.execute(Command(command=command, shell=True, check=False))
        output = (result.stdout or "").strip()
        removed = [line.split("=", 1)[1] for line in output.splitlines() if line.startswith("removed=")]
        logger.info(
            f"[{self.type}] [{runtime._config.host}] ray_log_cleanup done: "
            f"removed={len(removed)} sessions, output_head={output[:300]}"
        )
        return {
            "status": TaskStatusEnum.SUCCESS,
            "exit_code": result.exit_code,
            "removed_count": len(removed),
            "removed_sessions": removed,
            "output_head": output[:1000],
        }
