from datetime import datetime, timezone

from rock import env_vars
from rock.actions import ArchiveLogDirRequest
from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.monitor import MetricsMonitor
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import RockConfig
from rock.deployments.log_cleanup import LOG_STOPPED_SENTINEL
from rock.deployments.log_cleanup_sentinel import SentinelState
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="sandbox_log_archive", file_name=SCHEDULER_LOG_NAME)


class SandboxLogArchiveTask(BaseTask):
    """Deferred archive of stopped sandbox log dirs.

    Per-worker daily run:
      1. Walk ${ROCK_LOGGING_PATH}/* on the worker (via /execute shell)
         to find dirs containing the sentinel file
      2. For each, read sentinel via /read_file; skip if
         `now - stopped_at < keep_days`
      3. Otherwise dispatch /archive_log_dir to the worker. Worker
         performs tar+upload+rmtree (success) or bump_attempts (failure).
      4. After max_attempts the worker returns `failed_persist` and
         leaves the dir on disk; FileCleanupTask is the eventual janitor.
    """

    def __init__(
        self,
        interval_seconds: int = 86400,
        log_root: str | None = None,
    ):
        super().__init__(
            type="sandbox_log_archive",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.log_root = log_root or env_vars.ROCK_LOGGING_PATH
        self.metrics_monitor = MetricsMonitor.create(metric_prefix="disk_governance")

    @classmethod
    def from_config(cls, task_config) -> "SandboxLogArchiveTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            log_root=task_config.params.get("log_root"),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        if not self.log_root:
            logger.warning(f"[{self.type}] ROCK_LOGGING_PATH is empty; skip")
            return {"status": TaskStatusEnum.SUCCESS, "message": "no log root configured"}

        # 1) discover candidate dirs on the worker
        cmd = (
            f'find "{self.log_root}" -maxdepth 2 -mindepth 2 '
            f'-type f -name "{LOG_STOPPED_SENTINEL}" -printf "%h\\n" 2>/dev/null || true'
        )
        result = await runtime.execute(Command(command=cmd, shell=True, check=False))
        candidate_dirs = [d for d in (result.stdout or "").splitlines() if d.strip()]

        # 2) read cluster config (admin side) to make age + max-attempts decisions
        oss = RockConfig.from_env().oss
        keep_days = int(oss.keep_days_before_archive or 3)
        max_attempts = int(oss.archive_max_attempts or 3)

        archived = 0
        skipped_too_young = 0
        failed_pending = 0
        failed_persist = 0
        skipped_no_sentinel = 0

        for log_dir in candidate_dirs:
            outcome = await self._process_one(
                runtime=runtime,
                log_dir=log_dir,
                keep_days=keep_days,
                max_attempts=max_attempts,
            )
            if outcome == "archived":
                archived += 1
            elif outcome == "too_young":
                skipped_too_young += 1
            elif outcome == "failed_pending":
                failed_pending += 1
                self._emit(MetricsConstants.SANDBOX_LOG_ARCHIVE_PENDING, log_dir, 1)
            elif outcome == "failed_persist":
                failed_persist += 1
                self._emit(MetricsConstants.SANDBOX_LOG_ARCHIVE_FAILED_PERSIST, log_dir, 1)
            elif outcome == "skipped_no_sentinel":
                skipped_no_sentinel += 1

        return {
            "status": TaskStatusEnum.SUCCESS,
            "archived": archived,
            "skipped_too_young": skipped_too_young,
            "failed_pending": failed_pending,
            "failed_persist": failed_persist,
            "skipped_no_sentinel": skipped_no_sentinel,
        }

    async def _process_one(
        self,
        runtime: RemoteSandboxRuntime,
        log_dir: str,
        keep_days: int,
        max_attempts: int,
    ) -> str:
        """Returns one of: archived / too_young / failed_pending / failed_persist / skipped_no_sentinel."""
        # 2a) read sentinel via /read_file (small JSON, fits in HTTP body)
        sentinel_path = f"{log_dir.rstrip('/')}/{LOG_STOPPED_SENTINEL}"
        try:
            resp = await runtime.read_file(ReadFileRequest(path=sentinel_path))
        except Exception as e:
            logger.warning(f"[{self.type}] failed to read sentinel at {sentinel_path}: {e}")
            return "skipped_no_sentinel"

        if not resp.content:
            return "skipped_no_sentinel"

        try:
            import json as _json

            data = _json.loads(resp.content)
            state = SentinelState(
                stopped_at=data["stopped_at"],
                attempts=int(data.get("attempts", 0)),
                version=int(data.get("version", 1)),
            )
        except Exception as e:
            logger.warning(f"[{self.type}] sentinel parse failed for {sentinel_path}: {e}")
            return "skipped_no_sentinel"

        # 2b) age check on admin
        try:
            stopped_at = datetime.fromisoformat(state.stopped_at)
        except Exception:
            stopped_at = datetime.now(timezone.utc)
        age_days = (datetime.now(stopped_at.tzinfo or timezone.utc) - stopped_at).days
        if age_days < keep_days:
            return "too_young"

        # 2c) dispatch to worker
        container_name = log_dir.rstrip("/").split("/")[-1]
        archive_resp = await runtime.archive_log_dir(
            ArchiveLogDirRequest(
                log_dir=log_dir,
                container_name=container_name,
                max_attempts=max_attempts,
            )
        )
        return archive_resp.outcome

    def _emit(self, metric_name: str, log_dir: str, value: int) -> None:
        if not self.metrics_monitor:
            return
        container = log_dir.rstrip("/").split("/")[-1] or "unknown"
        self.metrics_monitor.record_counter_by_name(metric_name, value, {"container": container})
