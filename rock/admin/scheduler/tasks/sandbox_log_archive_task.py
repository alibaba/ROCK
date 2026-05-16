"""Deferred archival of stopped sandbox log dirs.

Per-worker daily run:
  1. /execute: walk ${ROCK_LOGGING_PATH}/* on the worker (find -name sentinel)
  2. /read_file: read each sentinel JSON; skip when age < keep_days
  3. /execute: tar | ossutil cp && rm -rf  (AK/SK via SandboxCommand.env,
     never argv) — single shell pipeline, no new RPC endpoint
  4. /write_file: on failure, bump attempts in the sentinel JSON; once
     attempts >= max_attempts, return failed_persist and leave the dir
     for FileCleanupTask to eventually reap.
"""

import json
from datetime import datetime, timezone

from rock import env_vars
from rock.actions import ReadFileRequest, WriteFileRequest
from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.monitor import MetricsMonitor
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import RockConfig
from rock.deployments.log_cleanup_sentinel import (
    LOG_STOPPED_SENTINEL,
    SentinelState,
    dump_state,
)
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.archive_command import build_archive_command, build_sandbox_log_key

logger = init_logger(name="sandbox_log_archive", file_name=SCHEDULER_LOG_NAME)


class SandboxLogArchiveTask(BaseTask):
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

        oss = RockConfig.from_env().oss
        primary = oss.primary
        bucket = primary.bucket or env_vars.ROCK_OSS_BUCKET_NAME or oss.bucket
        endpoint = primary.endpoint or env_vars.ROCK_OSS_BUCKET_ENDPOINT or oss.endpoint
        access_key_id = primary.access_key_id or oss.access_key_id
        access_key_secret = primary.access_key_secret or oss.access_key_secret

        if not (bucket and endpoint and access_key_id and access_key_secret):
            logger.warning(f"[{self.type}] OSS primary account incomplete; skip archival")
            return {"status": TaskStatusEnum.SUCCESS, "message": "oss primary account not configured"}

        keep_days = int(oss.keep_days_before_archive or 3)
        max_attempts = int(oss.archive_max_attempts or 3)
        archive_prefix = oss.archive_prefix or ""

        candidate_dirs = await self._discover_candidates(runtime)

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
                archive_prefix=archive_prefix,
                bucket=bucket,
                endpoint=endpoint,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
            if outcome == "archived":
                archived += 1
            elif outcome == "too_young":
                skipped_too_young += 1
            elif outcome == "failed_pending":
                failed_pending += 1
            elif outcome == "failed_persist":
                failed_persist += 1
                self._emit_failed_persist(log_dir)
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

    async def _discover_candidates(self, runtime: RemoteSandboxRuntime) -> list[str]:
        cmd = (
            f'find "{self.log_root}" -maxdepth 2 -mindepth 2 '
            f'-type f -name "{LOG_STOPPED_SENTINEL}" -exec dirname {{}} \\; 2>/dev/null || true'
        )
        result = await runtime.execute(Command(command=cmd, shell=True, check=False))
        return list(dict.fromkeys(d for d in (result.stdout or "").splitlines() if d.strip()))

    async def _process_one(
        self,
        runtime: RemoteSandboxRuntime,
        log_dir: str,
        keep_days: int,
        max_attempts: int,
        archive_prefix: str,
        bucket: str,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> str:
        """Returns one of: archived / too_young / failed_pending / failed_persist / skipped_no_sentinel."""
        sentinel_remote_path = f"{log_dir.rstrip('/')}/{LOG_STOPPED_SENTINEL}"
        state = await self._read_sentinel(runtime, sentinel_remote_path)
        if state is None:
            return "skipped_no_sentinel"

        try:
            stopped_at = datetime.fromisoformat(state.stopped_at)
        except Exception:
            stopped_at = datetime.now(timezone.utc)
        age_days = (datetime.now(stopped_at.tzinfo or timezone.utc) - stopped_at).days
        if age_days < keep_days:
            return "too_young"

        sandbox_id = log_dir.rstrip("/").split("/")[-1] or "unknown"
        oss_key = build_sandbox_log_key(sandbox_id, archive_prefix)
        cmd_str = build_archive_command(
            log_dir=log_dir,
            oss_key=oss_key,
            bucket=bucket,
            endpoint=endpoint,
        )
        result = await runtime.execute(
            Command(
                command=cmd_str,
                shell=True,
                check=False,
                env={
                    "OSS_ACCESS_KEY_ID": access_key_id,
                    "OSS_ACCESS_KEY_SECRET": access_key_secret,
                },
            )
        )
        if (result.exit_code or 0) == 0:
            return "archived"

        new_attempts = state.attempts + 1
        new_state = SentinelState(
            stopped_at=state.stopped_at,
            attempts=new_attempts,
            version=state.version,
        )
        try:
            await runtime.write_file(
                WriteFileRequest(
                    path=sentinel_remote_path,
                    content=dump_state(new_state),
                )
            )
        except Exception as e:
            logger.warning(f"[{self.type}] sentinel bump failed for {sentinel_remote_path}: {e}")

        if new_attempts >= max_attempts:
            logger.error(
                f"[{self.type}] archive gave up after {new_attempts} attempts: "
                f"{log_dir} (stderr={result.stderr[:200]!r})"
            )
            return "failed_persist"
        return "failed_pending"

    async def _read_sentinel(self, runtime: RemoteSandboxRuntime, path: str) -> SentinelState | None:
        try:
            resp = await runtime.read_file(ReadFileRequest(path=path))
        except Exception as e:
            logger.warning(f"[{self.type}] failed to read sentinel at {path}: {e}")
            return None
        if not resp.content:
            return None
        try:
            data = json.loads(resp.content)
            return SentinelState(
                stopped_at=data["stopped_at"],
                attempts=int(data.get("attempts", 0)),
                version=int(data.get("version", 1)),
            )
        except Exception as e:
            logger.warning(f"[{self.type}] sentinel parse failed for {path}: {e}")
            return None

    def _emit_failed_persist(self, log_dir: str) -> None:
        if not self.metrics_monitor:
            return
        sandbox_id = log_dir.rstrip("/").split("/")[-1] or "unknown"
        self.metrics_monitor.record_counter_by_name(
            MetricsConstants.SANDBOX_LOG_ARCHIVE_FAILED_PERSIST,
            1,
            {"sandbox_id": sandbox_id},
        )
