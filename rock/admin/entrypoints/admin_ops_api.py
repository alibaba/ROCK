"""Admin ops API: submit and query ops jobs (DB-backed, multi-pod safe).

Endpoints
- POST /apis/v1/admin/ops/jobs              submit a job
- GET  /apis/v1/admin/ops/jobs/{job_id}     query job state

Design notes
- All responses return ResponseStatus.SUCCESS once the server has processed
  the request (including rejection / rate-limit / not-found). Business state
  goes in ``result.status``.
- Job state is persisted in PostgreSQL via OpsJobTable so multi-pod admin
  deployments work consistently (no process-local dict).
- Whitelist (suffix ``_cleanup`` / ``_prune`` / ``_archive``) and 60s
  cross-pod rate limit protect against misuse during incidents.
- Registered only when ``--role=admin``, never on proxy.
"""

import asyncio
import os
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Request

from rock.actions.response import ResponseStatus, RockResponse
from rock.admin.core.ops_job_table import (
    JOB_STATUS_ACCEPTED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_NOT_FOUND,
    JOB_STATUS_RATE_LIMITED,
    JOB_STATUS_REJECTED,
    JOB_STATUS_RUNNING,
)
from rock.admin.proto.request import OpsJobRequest
from rock.admin.scheduler.task_base import BaseTask
from rock.common.exception import handle_exceptions
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.admin.core.ops_job_table import OpsJobTable

logger = init_logger(__name__)
audit_logger = init_logger("admin_ops_audit")

admin_ops_router = APIRouter()

# Cross-pod rate-limit window (seconds). Same task can't be re-submitted within
# this window. Stored in DB (via OpsJobTable.list_recent_by_tasks), not in
# process memory, so the gate applies across all admin pods.
_RATE_LIMIT_SECONDS = 60

# Whitelist suffixes — only tasks ending in these may be triggered via this API.
# Kept in lockstep with the description of OpsJobRequest.tasks in
# rock/admin/proto/request.py (single source of truth lives here).
_WHITELIST_SUFFIXES = ("_cleanup", "_prune", "_archive")


# dependency injection (set by main.py at lifespan startup)
# Providers are callables so we always read the *current* registry / workers,
# not a stale snapshot — scheduler thread mutates _tasks_by_class on Nacos
# config reload, and worker IPs change over time.
_ops_job_table: "OpsJobTable | None" = None
_task_registry_provider = None  # callable[[], dict[str, BaseTask]]
_alive_workers_provider = None  # callable[[], list[str]]


def set_ops_job_table(table: "OpsJobTable | None") -> None:
    global _ops_job_table
    _ops_job_table = table


def set_task_registry_provider(provider) -> None:
    global _task_registry_provider
    _task_registry_provider = provider


def set_alive_workers_provider(provider) -> None:
    global _alive_workers_provider
    _alive_workers_provider = provider


def _current_registry() -> dict[str, BaseTask]:
    if _task_registry_provider is None:
        return {}
    try:
        return _task_registry_provider() or {}
    except Exception as e:
        logger.warning(f"task registry provider failed: {e}")
        return {}


def _is_whitelisted(task_type: str) -> bool:
    return any(task_type.endswith(s) for s in _WHITELIST_SUFFIXES)


def _resolve_tasks(requested: list[str] | None) -> tuple[list[BaseTask], list[str]]:
    """Return (allowed_tasks, rejected_types).

    requested=None means "all whitelisted tasks in registry".
    """
    registry = _current_registry()
    if requested is None:
        return [t for name, t in registry.items() if _is_whitelisted(name)], []
    allowed: list[BaseTask] = []
    rejected: list[str] = []
    for name in requested:
        if not _is_whitelisted(name):
            rejected.append(name)
            continue
        if name not in registry:
            rejected.append(name)
            continue
        allowed.append(registry[name])
    return allowed, rejected


def _pod_id() -> str:
    return os.environ.get("HOSTNAME") or "unknown"


@admin_ops_router.post("/admin/ops/jobs")
@handle_exceptions(error_message="submit ops job failed")
async def submit_ops_job(
    request: Request,
    payload: OpsJobRequest = Body(default_factory=OpsJobRequest),
) -> RockResponse[dict]:
    """Submit an ops job. Always returns SUCCESS once the server has processed
    the request. Business state goes in ``result.status``:
      - accepted       — job stored in DB, background execution started
      - rate_limited   — all requested tasks within cooldown window
      - rejected       — all requested tasks failed whitelist
    """
    caller = request.client.host if request.client else "unknown"
    audit_logger.info(f"submit_ops_job: caller={caller}, tasks={payload.tasks}, workers={payload.worker_ips}")

    if _ops_job_table is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="ops job table not initialised",
            error="server misconfigured",
        )

    # 1) Resolve worker IPs
    if payload.worker_ips is not None:
        worker_ips = list(payload.worker_ips)
    elif _alive_workers_provider is not None:
        try:
            worker_ips = list(_alive_workers_provider())
        except Exception as e:
            logger.warning(f"alive workers provider failed: {e}")
            worker_ips = []
    else:
        worker_ips = []

    # 2) Resolve tasks against whitelist + registry
    allowed, rejected = _resolve_tasks(payload.tasks)

    if not allowed:
        return RockResponse(
            status=ResponseStatus.SUCCESS,
            message="ok",
            result={
                "job_id": None,
                "status": JOB_STATUS_REJECTED,
                "rejected_tasks": rejected,
            },
        )

    # 3) Cross-pod rate limit via DB query (last 60s)
    requested_types = [t.type for t in allowed]
    recent = await _ops_job_table.list_recent_by_tasks(requested_types, since_epoch=time.time() - _RATE_LIMIT_SECONDS)
    in_cooldown: set[str] = set()
    for r in recent:
        in_cooldown.update(set(r.get("tasks") or []) & set(requested_types))

    runnable = [t for t in allowed if t.type not in in_cooldown]
    rate_limited = sorted(in_cooldown)

    if not runnable:
        return RockResponse(
            status=ResponseStatus.SUCCESS,
            message="ok",
            result={
                "job_id": None,
                "status": JOB_STATUS_RATE_LIMITED,
                "rate_limited_tasks": rate_limited,
                "cooldown_seconds": _RATE_LIMIT_SECONDS,
                "rejected_tasks": rejected,
            },
        )

    # 4) Persist + fire-and-forget
    job_id = uuid.uuid4().hex[:12]
    submitted_at = time.time()
    pod_id = _pod_id()
    await _ops_job_table.insert(
        {
            "job_id": job_id,
            "submitted_by": caller,
            "tasks": [t.type for t in runnable],
            "worker_ips": worker_ips,
            "status": JOB_STATUS_ACCEPTED,
            "submitted_at": submitted_at,
            "pod_id": pod_id,
        }
    )
    asyncio.create_task(_run_job_async(job_id, runnable, worker_ips))
    audit_logger.info(
        f"submit_ops_job accepted: job_id={job_id}, caller={caller}, "
        f"tasks={[t.type for t in runnable]}, workers={len(worker_ips)}, pod={pod_id}"
    )

    return RockResponse(
        status=ResponseStatus.SUCCESS,
        message="ok",
        result={
            "job_id": job_id,
            "status": JOB_STATUS_ACCEPTED,
            "tasks": [t.type for t in runnable],
            "worker_count": len(worker_ips),
            "rejected_tasks": rejected,
            "rate_limited_tasks": rate_limited,
            "submitted_at": submitted_at,
            "pod_id": pod_id,
        },
    )


@admin_ops_router.get("/admin/ops/jobs/{job_id}")
@handle_exceptions(error_message="get ops job failed")
async def get_ops_job(job_id: str) -> RockResponse[dict]:
    """Query ops job state. SUCCESS even when not found (result.status='not_found')."""
    if _ops_job_table is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="ops job table not initialised",
            error="server misconfigured",
        )

    job = await _ops_job_table.get(job_id)
    if job is None:
        return RockResponse(
            status=ResponseStatus.SUCCESS,
            message="ok",
            result={"job_id": job_id, "status": JOB_STATUS_NOT_FOUND},
        )
    return RockResponse(
        status=ResponseStatus.SUCCESS,
        message="ok",
        result=job,
    )


async def _run_job_async(job_id: str, tasks: list[BaseTask], worker_ips: list[str]) -> None:
    """Execute tasks on workers and persist outcome to DB. Best-effort: if DB
    update fails we still log so operators can diagnose via audit log."""
    await _ops_job_table.update_status(job_id, JOB_STATUS_RUNNING)
    try:
        results: dict[str, list] = {}
        for task in tasks:
            try:
                await task.run(worker_ips)
                results[task.type] = [{"ip": ip, "ok": True} for ip in worker_ips]
            except Exception as e:
                logger.exception(f"ops job '{job_id}' task '{task.type}' failed")
                results[task.type] = [{"ip": ip, "ok": False, "error": str(e)} for ip in worker_ips]
        await _ops_job_table.update_status(job_id, JOB_STATUS_COMPLETED, results=results)
        audit_logger.info(f"ops job '{job_id}' completed")
    except Exception as e:
        logger.exception(f"ops job '{job_id}' failed catastrophically")
        await _ops_job_table.update_status(job_id, JOB_STATUS_FAILED, error=str(e))
