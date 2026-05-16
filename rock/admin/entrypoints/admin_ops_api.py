"""Admin operations API — internal endpoints for SRE on-call.

Currently provides:
- Disk emergency cleanup: trigger scheduled cleanup tasks immediately on
  selected workers, bypassing the scheduler interval.

Default mode is **async** (fire-and-forget): the API returns a job_id
immediately while tasks execute in the background. Callers can use mode="sync"
to block and get per-task summaries directly.

NOTE: Task instances are constructed on demand from RockConfig (via
TaskFactory). This does NOT touch the SchedulerThread's task registry, so
the two paths are independent — emergency cleanup works even when the
periodic scheduler is intentionally disabled.
"""

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request

from rock.actions import ResponseStatus, RockResponse
from rock.admin.proto.request import DiskEmergencyCleanupRequest
from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_factory import TaskFactory
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.common.exception import handle_exceptions
from rock.config import RockConfig
from rock.logger import init_logger

audit_logger = init_logger(name="admin_ops", file_name=SCHEDULER_LOG_NAME)
logger = init_logger(name="admin_ops_api")

admin_ops_router = APIRouter()

# --- Whitelist & rate limit (process-local) ---

_TASK_WHITELIST_SUFFIXES: tuple[str, ...] = ("_cleanup", "_prune")
_RATE_LIMIT_SECONDS: int = 60
_last_triggered_at: dict[str, float] = {}  # task_type -> unix ts

# Wired from main.py lifespan. Decoupled from SchedulerThread.
_alive_workers_provider = None
_rock_config_provider = None

# In-memory store for async job status tracking
_async_jobs: dict[str, dict[str, Any]] = {}


def set_alive_workers_provider(provider) -> None:
    global _alive_workers_provider
    _alive_workers_provider = provider


def set_rock_config_provider(provider) -> None:
    """Wire a callable that returns the current RockConfig (Nacos hot-reload friendly)."""
    global _rock_config_provider
    _rock_config_provider = provider


def _is_task_whitelisted(task_type: str) -> bool:
    return any(task_type.endswith(sfx) for sfx in _TASK_WHITELIST_SUFFIXES)


def _build_eligible_tasks(rock_config: RockConfig) -> dict[str, BaseTask]:
    """Construct BaseTask instances for every whitelisted yml entry.

    Builds on demand, so config changes (Nacos / yml reload) are picked up
    on the next API call without any cross-thread state.
    """
    out: dict[str, BaseTask] = {}
    for task_config in rock_config.scheduler.tasks:
        if not getattr(task_config, "task_class", None):
            continue
        # Even disabled-in-yml cleanup tasks should be reachable by emergency
        # trigger (the whole point is "scheduler off, but SRE needs it now").
        try:
            task = TaskFactory.create_task(task_config)
        except Exception as e:
            logger.warning(
                f"emergency_cleanup: failed to construct task '{task_config.task_class}': {e}"
            )
            continue
        if _is_task_whitelisted(task.type):
            out[task.type] = task
    return out


def _select_tasks(
    requested: list[str] | None,
    available: dict[str, BaseTask],
) -> tuple[list[BaseTask], list[str]]:
    """Resolve names against the available (whitelisted) pool."""
    errors: list[str] = []
    if requested is None:
        return list(available.values()), errors

    selected: list[BaseTask] = []
    seen: set[str] = set()
    for name in requested:
        if name in seen:
            errors.append(f"{name}: duplicate in request")
            continue
        seen.add(name)
        if name not in available:
            errors.append(
                f"{name}: not in eligible task pool "
                f"(must be a yml-registered task whose type ends in {_TASK_WHITELIST_SUFFIXES})"
            )
            continue
        selected.append(available[name])
    return selected, errors


def _check_and_mark_rate_limit(task_type: str, now: float) -> bool:
    """Returns True if allowed; False if hit cooldown.
    Side effect on success: updates _last_triggered_at[task_type] = now.
    """
    last = _last_triggered_at.get(task_type, 0.0)
    if now - last < _RATE_LIMIT_SECONDS:
        return False
    _last_triggered_at[task_type] = now
    return True


async def _run_tasks_blocking(
    tasks: list[BaseTask],
    worker_ips: list[str],
) -> dict[str, Any]:
    """Run each task sequentially across workers; collect per-task summary."""
    results: dict[str, Any] = {}
    for task in tasks:
        t0 = time.time()
        try:
            await task.run(worker_ips)
            results[task.type] = {
                "status": "ok",
                "elapsed_seconds": round(time.time() - t0, 2),
                "worker_count": len(worker_ips),
            }
        except Exception as e:
            results[task.type] = {
                "status": "error",
                "error": str(e),
                "elapsed_seconds": round(time.time() - t0, 2),
                "worker_count": len(worker_ips),
            }
            logger.exception(f"emergency_cleanup task '{task.type}' failed: {e}")
    return results


async def _run_tasks_async_background(
    job_id: str,
    tasks: list[BaseTask],
    worker_ips: list[str],
    caller_ip: str,
) -> None:
    """Background coroutine for async mode; updates _async_jobs on completion."""
    try:
        results = await _run_tasks_blocking(tasks, worker_ips)
        _async_jobs[job_id]["status"] = "completed"
        _async_jobs[job_id]["results"] = results
        audit_logger.info(
            f"emergency_cleanup async job completed: job_id={job_id}, "
            f"caller={caller_ip}, results={results}"
        )
    except Exception as e:
        _async_jobs[job_id]["status"] = "failed"
        _async_jobs[job_id]["error"] = str(e)
        logger.exception(f"emergency_cleanup async job '{job_id}' failed: {e}")


@admin_ops_router.post("/disk_emergency_cleanup")
@handle_exceptions(error_message="disk emergency cleanup failed")
async def disk_emergency_cleanup(
    payload: DiskEmergencyCleanupRequest,
    request: Request,
) -> RockResponse[dict]:
    caller_ip = request.client.host if request.client else "unknown"
    audit_logger.info(
        f"emergency_cleanup called: caller={caller_ip}, "
        f"tasks={payload.tasks}, worker_ips={payload.worker_ips}"
    )

    # 1) Resolve workers
    if payload.worker_ips is not None:
        worker_ips = list(payload.worker_ips)
    elif _alive_workers_provider is not None:
        try:
            worker_ips = list(_alive_workers_provider())
        except Exception as e:
            logger.exception(f"alive_workers_provider raised: {e}")
            worker_ips = []
    else:
        worker_ips = []

    if not worker_ips:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="no worker available",
            error="worker_ips empty and alive-workers provider returned nothing",
            result=None,
        )

    # 2) Resolve eligible tasks from current RockConfig
    if _rock_config_provider is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="not initialized",
            error="rock_config_provider not wired; admin lifespan did not set it",
            result=None,
        )
    try:
        rock_config = _rock_config_provider()
    except Exception as e:
        logger.exception(f"rock_config_provider raised: {e}")
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="config unavailable",
            error=str(e),
            result=None,
        )

    available = _build_eligible_tasks(rock_config)
    selected, errors = _select_tasks(payload.tasks, available)
    if not selected:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="no eligible task",
            error="; ".join(errors) or "no whitelisted cleanup task registered in yml",
            result={"errors": errors, "available": sorted(available.keys())},
        )

    # 3) Per-task rate limit
    now = time.time()
    allowed: list[BaseTask] = []
    rate_limited: list[str] = []
    for task in selected:
        if _check_and_mark_rate_limit(task.type, now):
            allowed.append(task)
        else:
            rate_limited.append(task.type)

    if not allowed:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="rate limited",
            error=f"all requested tasks were triggered within last {_RATE_LIMIT_SECONDS}s",
            result={"rate_limited": rate_limited, "cooldown_seconds": _RATE_LIMIT_SECONDS},
        )

    # 4) Fire-and-forget: return job_id immediately
    job_id = uuid.uuid4().hex[:12]
    _async_jobs[job_id] = {
        "status": "running",
        "tasks": [t.type for t in allowed],
        "worker_count": len(worker_ips),
        "submitted_at": time.time(),
    }
    asyncio.create_task(
        _run_tasks_async_background(job_id, allowed, worker_ips, caller_ip)
    )
    audit_logger.info(
        f"emergency_cleanup dispatched: job_id={job_id}, caller={caller_ip}, "
        f"tasks={[t.type for t in allowed]}, workers={len(worker_ips)}"
    )
    return RockResponse(
        status=ResponseStatus.SUCCESS,
        message="accepted",
        result={
            "job_id": job_id,
            "tasks": [t.type for t in allowed],
            "worker_count": len(worker_ips),
            "rejected": errors,
            "rate_limited": rate_limited,
        },
    )


@admin_ops_router.get("/disk_emergency_cleanup/status/{job_id}")
@handle_exceptions(error_message="failed to get job status")
async def disk_emergency_cleanup_status(job_id: str) -> RockResponse[dict]:
    """Query the status of an async emergency cleanup job."""
    if job_id not in _async_jobs:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="job not found",
            error=f"job_id '{job_id}' does not exist or has expired",
            result=None,
        )
    return RockResponse(
        status=ResponseStatus.SUCCESS,
        message="ok",
        result=_async_jobs[job_id],
    )
