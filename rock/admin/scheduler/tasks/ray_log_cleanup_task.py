"""Clean up Ray temp dir on each worker.

Three layers of cleanup, run together in one shell pipeline:
  1. Stale full ``session_<ts>_<pid>`` dirs (excluding the live session).
  2. Inside ``session_latest/logs/``: per-file PID-aware cleanup
     (delete files whose PID is no longer alive) plus an mtime fallback
     for non-PID, non-daemon files.
  3. Inside ``session_latest/logs/old/``: time-based cleanup of Ray's
     own rotation backups.

Daemon-written files (raylet*, gcs_server*, runtime_env_agent*, etc.)
are NEVER deleted while the session is alive — Ray holds open fds, so
removing the inode wouldn't free disk and breaks log following.
"""

import textwrap

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="ray_log_cleanup", file_name=SCHEDULER_LOG_NAME)


class RayLogCleanupTask(BaseTask):
    """Clean Ray temp dir: stale session dirs + active session per-file cleanup.

    Ray restarts (cluster up/down, head failover) leave dozens of
    ``session_<timestamp>_<pid>`` dirs behind. The currently active one is
    symlinked as ``session_latest``; we resolve that link and skip its target
    when removing stale session dirs.

    Inside the live ``session_latest/logs/``, Ray accumulates per-worker
    logs (named after the worker PID) that are never reaped after the
    worker exits, plus its own rotation backups under ``logs/old/`` that
    Ray never cleans. Without intervention, file count grows to tens or
    hundreds of thousands on long-running clusters.

    NOTE: This is the WORKER side. The ray-head's /data/tmp/ray is cleaned
    by a daily cron baked into the head Dockerfile (rock-internal repo);
    rocklet is not deployed on the head and the worker scheduler does not
    reach it. The cron script mirrors this task's shell pipeline.
    """

    def __init__(
        self,
        interval_seconds: int = 86400,
        ray_temp_dir: str = "/data/tmp/ray",
        min_age_hours: int = 24,
        live_log_keep_days: int = 7,
        old_logs_keep_hours: int = 24,
        setup_log_keep_minutes: int = 60,
    ):
        """
        Args:
            interval_seconds: Execution interval, default 24 hours.
            ray_temp_dir: Ray's --temp-dir, default /data/tmp/ray.
            min_age_hours: Only delete session dirs whose mtime is older than
                this AND that are not session_latest. Default 24h.
            live_log_keep_days: Mtime threshold for non-PID, non-daemon files
                in session_latest/logs/. Default 7 days.
            old_logs_keep_hours: Mtime threshold for files under
                session_latest/logs/old/ (Ray's own rotation backups).
                Default 24 hours.
            setup_log_keep_minutes: Mtime threshold for runtime_env_setup-*
                files. These are one-shot Ray runtime-env materialisation
                scripts whose trailing token is a Ray job id (digit OR hex),
                NOT a Linux PID — PART 2a's PID probe is meaningless for them.
                Default 60min (matches PART 2a race-window guard).
        """
        super().__init__(
            type="ray_log_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        if min_age_hours < 1:
            raise ValueError(f"ray_log_cleanup.min_age_hours must be >= 1, got {min_age_hours}")
        if live_log_keep_days < 1:
            raise ValueError(f"ray_log_cleanup.live_log_keep_days must be >= 1, got {live_log_keep_days}")
        if old_logs_keep_hours < 1:
            raise ValueError(f"ray_log_cleanup.old_logs_keep_hours must be >= 1, got {old_logs_keep_hours}")
        if setup_log_keep_minutes < 1:
            raise ValueError(f"ray_log_cleanup.setup_log_keep_minutes must be >= 1, got {setup_log_keep_minutes}")
        self.ray_temp_dir = ray_temp_dir.rstrip("/")
        self.min_age_hours = min_age_hours
        self.live_log_keep_days = live_log_keep_days
        self.old_logs_keep_hours = old_logs_keep_hours
        self.setup_log_keep_minutes = setup_log_keep_minutes

    @classmethod
    def from_config(cls, task_config) -> "RayLogCleanupTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            ray_temp_dir=task_config.params.get("ray_temp_dir", "/data/tmp/ray"),
            min_age_hours=task_config.params.get("min_age_hours", 24),
            live_log_keep_days=task_config.params.get("live_log_keep_days", 7),
            old_logs_keep_hours=task_config.params.get("old_logs_keep_hours", 24),
            setup_log_keep_minutes=task_config.params.get("setup_log_keep_minutes", 60),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        ray_dir = self.ray_temp_dir
        session_age_min = self.min_age_hours * 60
        live_age_min = self.live_log_keep_days * 24 * 60
        old_age_min = self.old_logs_keep_hours * 60
        setup_age_min = self.setup_log_keep_minutes

        # 4-stage shell pipeline:
        #   PART 1  — drop stale full session_<ts>_<pid> dirs
        #   PART 2a — session_latest/logs/ PID-aware (worker/runtime_env_setup with digit pid)
        #   PART 2c — session_latest/logs/ runtime_env_setup-* mtime-only (covers hex variant)
        #   PART 2b — session_latest/logs/ non-PID, non-daemon stale (long tail)
        #   PART 3  — session_latest/logs/old/ time-based
        #
        # textwrap.dedent strips common leading whitespace so source can be
        # indented for readability without polluting the emitted shell.
        command = textwrap.dedent(
            f"""\
            set +e
            if [ ! -d "{ray_dir}" ]; then
              echo "ray_temp_dir_not_found"
              exit 0
            fi

            # PART 1: stale full session_<ts>_<pid> dirs (not session_latest)
            LIVE=$(readlink "{ray_dir}/session_latest" 2>/dev/null | xargs -I{{}} basename {{}} 2>/dev/null)
            echo "live_session=${{LIVE:-<none>}}"
            find "{ray_dir}" -maxdepth 1 -type d -name "session_*" \\
              ! -name "session_latest" -mmin +{session_age_min} \\
            | while read -r d; do
                bn=$(basename "$d")
                if [ "$bn" != "$LIVE" ]; then
                  rm -rf "$d" && echo "removed=$bn"
                fi
              done

            LOGS="{ray_dir}/session_latest/logs"
            if [ -d "$LOGS" ]; then
              # PART 2a: PID-aware — files matching *[_-]<pid>.{{log,err,out}}
              # Probe `kill -0 <pid>`; if PID is dead, remove. Only files older
              # than 60 minutes are considered, to avoid racing with new
              # worker startups still writing their first log line.
              #
              # Daemon files are excluded by name FIRST (same whitelist as
              # PART 2b). Without this guard, names like `agent-<id>.err` —
              # where <id> is a Ray-generated agent identifier, NOT a PID —
              # match the PID regex; kill -0 <id> fails because <id> exceeds
              # the Linux PID range, so the file gets wrongly removed even
              # though Ray's runtime env agent is still writing to it.
              find "$LOGS" -maxdepth 1 -type f -mmin +60 \\
                  -regextype posix-extended \\
                  -regex '.*[_-][0-9]+\\.(log|err|out)$' \\
                  ! -name 'raylet*' \\
                  ! -name 'gcs_server*' \\
                  ! -name 'runtime_env_agent*' \\
                  ! -name 'dashboard*' \\
                  ! -name 'monitor*' \\
                  ! -name 'log_monitor*' \\
                  ! -name 'agent-*' \\
              | while read -r f; do
                  bn=$(basename "$f")
                  pid=$(echo "$bn" | grep -oE '[_-][0-9]+\\.(log|err|out)$' | grep -oE '[0-9]+' | head -1)
                  [ -z "$pid" ] && continue
                  if ! kill -0 "$pid" 2>/dev/null; then
                    rm -f "$f" && echo "removed_dead_pid_log=$bn"
                  fi
                done

              # PART 2c: runtime_env_setup-* — one-shot Ray runtime-env
              # materialisation scripts. Trailing token is a Ray job id,
              # NOT a PID; comes in two flavours:
              #   - pure digits: `runtime_env_setup-31050000.log` — accidentally
              #     removed by PART 2a (digit > pid_max → kill -0 fails)
              #   - hex: `runtime_env_setup-f4060000.log` — skipped by PART 2a
              #     (regex needs digits) and waited 7d for PART 2b
              # Same retention window as PART 2a ({setup_age_min}min default)
              # to avoid races with in-flight setups while clearing the bulk.
              find "$LOGS" -maxdepth 1 -type f -mmin +{setup_age_min} \\
                  -name 'runtime_env_setup-*' \\
              | while read -r f; do
                  rm -f "$f" && echo "removed_setup=$(basename "$f")"
                done

              # PART 2b: non-PID, non-daemon stale files older than
              # {self.live_log_keep_days} days. Daemon files (raylet*,
              # gcs_server*, runtime_env_agent*, dashboard*, monitor*,
              # log_monitor*, agent-*) are NEVER deleted while session is
              # alive — Ray holds open fds, removal wouldn't free disk.
              find "$LOGS" -maxdepth 1 -type f -mmin +{live_age_min} \\
                  -regextype posix-extended \\
                  ! -regex '.*[_-][0-9]+\\.(log|err|out)$' \\
                  ! -name 'raylet*' \\
                  ! -name 'gcs_server*' \\
                  ! -name 'runtime_env_agent*' \\
                  ! -name 'dashboard*' \\
                  ! -name 'monitor*' \\
                  ! -name 'log_monitor*' \\
                  ! -name 'agent-*' \\
              | while read -r f; do
                  rm -f "$f" && echo "removed_stale_file=$(basename "$f")"
                done
            fi

            # PART 3: session_latest/logs/old/ — Ray's own rotation backups
            OLD="$LOGS/old"
            if [ -d "$OLD" ]; then
              find "$OLD" -type f -mmin +{old_age_min} \\
              | while read -r f; do
                  rm -f "$f" && echo "removed_old=$(basename "$f")"
                done
            fi

            echo "ray_log_cleanup_done"
            """
        )
        result = await runtime.execute(Command(command=command, shell=True, check=False))
        output = (result.stdout or "").strip()

        # Parse per-category removal counts from output.
        # `removed=<sess>` retained for backward compat (PART 1 session dirs).
        removed_sessions = [line.split("=", 1)[1] for line in output.splitlines() if line.startswith("removed=")]
        removed_dead_pid = sum(1 for line in output.splitlines() if line.startswith("removed_dead_pid_log="))
        removed_setup = sum(1 for line in output.splitlines() if line.startswith("removed_setup="))
        removed_stale = sum(1 for line in output.splitlines() if line.startswith("removed_stale_file="))
        removed_old = sum(1 for line in output.splitlines() if line.startswith("removed_old="))

        logger.info(
            f"[{self.type}] [{runtime._config.host}] ray_log_cleanup done: "
            f"sessions={len(removed_sessions)}, dead_pid={removed_dead_pid}, "
            f"setup={removed_setup}, stale={removed_stale}, old={removed_old}, "
            f"output_head={output[:300]}"
        )
        return {
            "status": TaskStatusEnum.SUCCESS,
            "exit_code": result.exit_code,
            # Backward-compatible fields (session-level removal):
            "removed_count": len(removed_sessions),
            "removed_sessions": removed_sessions,
            # New per-category counters:
            "removed_dead_pid_count": removed_dead_pid,
            "removed_setup_count": removed_setup,
            "removed_stale_count": removed_stale,
            "removed_old_count": removed_old,
            "output_head": output[:1500],
        }
