"""Deferred archival of stopped sandbox log directories — DB-driven.

Per-worker daily task. Each run:
  1. /execute on worker: ``ls ${log_root}/`` returns candidate sandbox_ids
     (directories under the log root are named after the sandbox they belong to).
  2. SandboxTable.list_by_in("sandbox_id", candidate_ids): batch query
     state + stop_time from sandbox_record.
  3. For each candidate:
       - state != "stopped"                 → skip (sandbox still alive)
       - stop_time missing / unparseable    → log warning, skip
       - age_days < keep_days_before_archive → skip (too young)
       - else                                → tar | ossutil cp && rm -rf

No sentinel files; single source of truth is sandbox_record.
Credentials are passed via SandboxCommand.env, never argv.
"""

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rock import env_vars
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.archive_command import ArchiveCommand

if TYPE_CHECKING:
    pass

logger = init_logger(name="sandbox_log_archive", file_name=SCHEDULER_LOG_NAME)


# Module-level providers, injected by main.py lifespan.
# Lazy callable so we always read the *current* config / table reference,
# not a stale snapshot — config can be hot-reloaded via Nacos.
_sandbox_table_provider = None  # callable[[], SandboxTable | None]
_rock_config_provider = None  # callable[[], RockConfig | None]
# Main event loop (uvicorn's loop where lifespan + HTTP handlers run).
# Required because this task runs inside SchedulerThread's child loop, but
# `sandbox_table` (asyncpg/SQLAlchemy) has a pool bound to the main loop —
# awaiting it directly from the child loop pollutes pool affinity and breaks
# subsequent HTTP handlers with "Future attached to a different loop".
_main_loop_provider = None  # callable[[], asyncio.AbstractEventLoop | None]


def set_sandbox_table_provider(provider) -> None:
    global _sandbox_table_provider
    _sandbox_table_provider = provider


def set_rock_config_provider(provider) -> None:
    global _rock_config_provider
    _rock_config_provider = provider


def set_main_loop_provider(provider) -> None:
    global _main_loop_provider
    _main_loop_provider = provider


# Safety cap on cross-loop dispatch: if the main loop is gone (e.g. admin
# was SIGKILLed before SchedulerThread had a chance to stop), the child
# loop's await on the dispatched future would hang forever. 60s is generous
# for any reasonable sandbox_table query; raises TimeoutError if exceeded.
_CROSS_LOOP_DISPATCH_TIMEOUT = 60.0


async def _run_on_main_loop(coro):
    """Dispatch ``coro`` to the main event loop if we're on a different one.

    Why: SchedulerThread runs tasks inside its own asyncio loop. asyncpg /
    SQLAlchemy pool is bound to whichever loop first uses it (the main loop,
    via lifespan ``create_tables`` + HTTP handlers). Calling ``await
    sandbox_table.xxx()`` directly from the scheduler's child loop binds the
    pool to *that* loop instead, breaking subsequent HTTP requests on the
    main loop with ``Future attached to a different loop``.

    The dispatched future is bounded by ``_CROSS_LOOP_DISPATCH_TIMEOUT``
    to prevent the child loop from hanging if the main loop has stopped.
    """
    main_loop = _main_loop_provider() if _main_loop_provider else None
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if main_loop is None or current is main_loop:
        # No main loop wired, or we're already on it — direct await is safe.
        return await coro
    # We're on a child loop. Dispatch to main loop and await via wrap_future,
    # bounded by a timeout so a dead main loop doesn't block us forever.
    future = asyncio.run_coroutine_threadsafe(coro, main_loop)
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=_CROSS_LOOP_DISPATCH_TIMEOUT)


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
        # Resolved at run time, not init time, so YAML override / env var
        # still has effect when ROCK_LOGGING_PATH is exported late.
        self._log_root_override = log_root

    @classmethod
    def from_config(cls, task_config) -> "SandboxLogArchiveTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            log_root=task_config.params.get("log_root"),
        )

    @property
    def log_root(self) -> str:
        root = self._log_root_override or env_vars.ROCK_LOGGING_PATH or ""
        return root.rstrip("/")

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        if not self.log_root:
            logger.warning(f"[{self.type}] log_root unconfigured (set ROCK_LOGGING_PATH); skip")
            return {"status": TaskStatusEnum.SUCCESS, "message": "no log root configured"}

        sandbox_table = _sandbox_table_provider() if _sandbox_table_provider else None
        if sandbox_table is None:
            logger.warning(f"[{self.type}] sandbox_table provider not set; skip")
            return {"status": TaskStatusEnum.SUCCESS, "message": "sandbox_table not available"}

        rock_config = _rock_config_provider() if _rock_config_provider else None
        if rock_config is None:
            logger.warning(f"[{self.type}] rock_config provider not set; skip")
            return {"status": TaskStatusEnum.SUCCESS, "message": "rock_config not available"}

        primary = rock_config.oss.primary
        bucket = primary.bucket
        endpoint = primary.endpoint
        access_key_id = primary.access_key_id
        access_key_secret = primary.access_key_secret
        if not (bucket and endpoint and access_key_id and access_key_secret):
            logger.warning(f"[{self.type}] OSS primary account incomplete; skip archival")
            return {"status": TaskStatusEnum.SUCCESS, "message": "oss primary account not configured"}

        log_cfg = rock_config.sandbox_config.log
        keep_days = int(log_cfg.keep_days_before_archive or 3)
        archive_prefix = log_cfg.archive_prefix or ""

        # Step 1: discover candidate sandbox_ids on this worker
        candidate_ids = await self._discover_candidates(runtime)
        if not candidate_ids:
            return {
                "status": TaskStatusEnum.SUCCESS,
                "scanned": 0,
                "archived": 0,
            }

        # Step 2: batch query DB for state + stop_time
        # Dispatch DB query to main loop (see _run_on_main_loop docstring).
        rows = await _run_on_main_loop(sandbox_table.list_by_in("sandbox_id", candidate_ids))
        rows_by_id = {r["sandbox_id"]: r for r in rows}

        now = datetime.now(timezone.utc)
        archived = 0
        skipped_alive = 0
        skipped_too_young = 0
        skipped_orphan = 0
        failed = 0

        for sandbox_id in candidate_ids:
            row = rows_by_id.get(sandbox_id)
            if row is None:
                logger.warning(f"[{self.type}] orphan log dir for {sandbox_id} (no DB row); skip")
                skipped_orphan += 1
                continue
            if row.get("state") != "stopped":
                skipped_alive += 1
                continue

            stop_time = self._parse_stop_time(row.get("stop_time"))
            if stop_time is None:
                logger.warning(f"[{self.type}] {sandbox_id} state=stopped but stop_time missing/unparseable; skip")
                skipped_orphan += 1
                continue

            age_days = (now - stop_time).days
            if age_days < keep_days:
                skipped_too_young += 1
                continue

            try:
                await self._archive_one(
                    runtime,
                    sandbox_id,
                    archive_prefix,
                    bucket,
                    endpoint,
                    access_key_id,
                    access_key_secret,
                )
                archived += 1
            except Exception as e:
                logger.exception(f"[{self.type}] archive {sandbox_id} failed: {e}")
                failed += 1

        return {
            "status": TaskStatusEnum.SUCCESS,
            "scanned": len(candidate_ids),
            "archived": archived,
            "skipped_alive": skipped_alive,
            "skipped_too_young": skipped_too_young,
            "skipped_orphan": skipped_orphan,
            "failed": failed,
        }

    async def _discover_candidates(self, runtime: RemoteSandboxRuntime) -> list[str]:
        """List sandbox_ids that still have a log directory on this worker.

        Top-level entries under ``log_root`` are treated as candidate
        sandbox_ids. Non-sandbox entries (e.g. logrotate's own files) are
        filtered out by the DB lookup step (Step 2) — orphans are logged.
        """
        cmd = f"ls -1 {self.log_root} 2>/dev/null || true"
        result = await runtime.execute(Command(command=cmd, shell=True, check=False))
        if result.exit_code != 0:
            return []
        names = (result.stdout or "").strip().split("\n")
        return [n.strip() for n in names if n.strip()]

    @staticmethod
    def _parse_stop_time(raw) -> datetime | None:
        """Parse stop_time from DB row. Returns None on missing/malformed.

        ``sandbox_record.stop_time`` is ``String(64)``; accept ISO 8601 with
        or without timezone. Naive datetimes are assumed UTC.
        """
        if not raw:
            return None
        try:
            s = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    async def _archive_one(
        self,
        runtime: RemoteSandboxRuntime,
        sandbox_id: str,
        archive_prefix: str,
        bucket: str,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        """tar+gzip the sandbox's log dir, upload via ossutil, rm -rf on success.

        Credentials go through ``Command.env`` so they never appear in argv /
        ``ps`` output. The command itself is built by the pure-function
        ``build_archive_command`` (PR #957) — single source of truth for
        archive key naming.
        """
        log_dir = f"{self.log_root}/{sandbox_id}"
        oss_key = ArchiveCommand.build_key(sandbox_id, archive_prefix)
        cmd = ArchiveCommand.build_command(log_dir, oss_key, bucket, endpoint)
        await runtime.execute(
            Command(
                command=cmd,
                shell=True,
                check=True,
                env={
                    "OSS_ACCESS_KEY_ID": access_key_id,
                    "OSS_ACCESS_KEY_SECRET": access_key_secret,
                },
            )
        )
        logger.info(f"[{self.type}] archived {sandbox_id} -> oss://{bucket}/{oss_key}")
