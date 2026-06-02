"""Admin ops API: create and query TaskSets (DB-backed, multi-pod safe).

Endpoints (relative to prefix /apis/envs/sandbox/v1/ops):
- POST /tasksets             Create a TaskSet (triggers tasks on workers)
- GET  /tasksets/{taskset_id}  Query TaskSet with child tasks

Single-table design: one row per task_type per API call, grouped by taskset_id.
"""

import asyncio
import os
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from rock.actions.response import ResponseStatus, RockResponse
from rock.admin.core.scheduler_task_table import (
    PHASE_FAILED,
    PHASE_NOT_FOUND,
    PHASE_PENDING,
    PHASE_RATE_LIMITED,
    PHASE_REJECTED,
    PHASE_RUNNING,
    PHASE_SUCCEEDED,
)
from rock.admin.proto.request import CreateTaskSetRequest, TaskSetSpec
from rock.admin.scheduler.task_base import BaseTask
from rock.common.exception import handle_exceptions
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.admin.core.scheduler_task_table import SchedulerTaskTable

logger = init_logger(__name__)
audit_logger = init_logger("admin_ops_audit")

admin_ops_router = APIRouter()

_RATE_LIMIT_SECONDS = 60
_WHITELIST_SUFFIXES = ("_cleanup", "_prune", "_archive")


class TaskSetMetadata(BaseModel):
    tasksetId: str
    creationTimestamp: float


class TaskSetStatusModel(BaseModel):
    phase: str
    assignedPod: str = ""
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    startTime: float | None = None
    completionTime: float | None = None


class TaskMetadata(BaseModel):
    taskId: str
    tasksetId: str
    creationTimestamp: float


class TaskStatusModel(BaseModel):
    phase: str
    startTime: float | None = None
    completionTime: float | None = None
    conditions: list[dict] | None = None
    result: list[dict] | None = None


class TaskResponse(BaseModel):
    metadata: TaskMetadata
    spec: dict
    status: TaskStatusModel


class TaskSetResponse(BaseModel):
    metadata: TaskSetMetadata
    spec: TaskSetSpec
    status: TaskSetStatusModel
    tasks: list[TaskResponse] | None = None


# --- Dependency injection ---

_task_table: "SchedulerTaskTable | None" = None
_task_registry_provider = None
_alive_workers_provider = None


def set_scheduler_task_table(table: "SchedulerTaskTable | None") -> None:
    global _task_table
    _task_table = table


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


def _aggregate_taskset(taskset_id: str, tasks: list[dict]) -> TaskSetResponse:
    """Build a TaskSetResponse by aggregating task rows."""
    task_responses = [
        TaskResponse(
            metadata=TaskMetadata(
                taskId=t["task_id"],
                tasksetId=t["taskset_id"],
                creationTimestamp=t["creation_timestamp"],
            ),
            spec={"taskType": t["task_type"], "targetWorkers": t["target_workers"]},
            status=TaskStatusModel(
                phase=t["phase"],
                startTime=t.get("start_time"),
                completionTime=t.get("completion_time"),
                conditions=t.get("conditions"),
                result=t.get("result"),
            ),
        )
        for t in tasks
    ]

    succeeded = sum(1 for t in tasks if t["phase"] == PHASE_SUCCEEDED)
    failed = sum(1 for t in tasks if t["phase"] == PHASE_FAILED)
    active = len(tasks) - succeeded - failed

    if active > 0:
        phase = PHASE_RUNNING
    elif failed > 0:
        phase = PHASE_FAILED
    else:
        phase = PHASE_SUCCEEDED

    start_times = [t["start_time"] for t in tasks if t.get("start_time")]
    completion_times = [t["completion_time"] for t in tasks if t.get("completion_time")]

    return TaskSetResponse(
        metadata=TaskSetMetadata(
            tasksetId=taskset_id,
            creationTimestamp=min(t["creation_timestamp"] for t in tasks),
        ),
        spec=TaskSetSpec(
            targetWorkers=tasks[0]["target_workers"] if tasks else None,
        ),
        status=TaskSetStatusModel(
            phase=phase,
            assignedPod=tasks[0].get("assigned_pod", "") if tasks else "",
            active=active,
            succeeded=succeeded,
            failed=failed,
            startTime=min(start_times) if start_times else None,
            completionTime=max(completion_times) if completion_times and active == 0 else None,
        ),
        tasks=task_responses,
    )


# --- Endpoints ---


@admin_ops_router.post("/tasksets")
@handle_exceptions(error_message="create taskset failed")
async def create_taskset(
    request: Request,
    payload: CreateTaskSetRequest = Body(default_factory=CreateTaskSetRequest),
) -> RockResponse[dict]:
    caller = request.client.host if request.client else "unknown"
    audit_logger.info(f"create_taskset: caller={caller}, spec={payload.spec.model_dump()}")

    if _task_table is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="scheduler task table not initialised",
            error="server misconfigured",
        )

    # 1) Resolve worker IPs
    if payload.spec.targetWorkers is not None:
        worker_ips = list(payload.spec.targetWorkers)
    elif _alive_workers_provider is not None:
        try:
            worker_ips = list(_alive_workers_provider())
        except Exception as e:
            logger.warning(f"alive workers provider failed: {e}")
            worker_ips = []
    else:
        worker_ips = []

    # 2) Resolve tasks against whitelist + registry
    allowed, rejected = _resolve_tasks(payload.spec.taskTypes)

    if not allowed:
        resp = TaskSetResponse(
            metadata=TaskSetMetadata(tasksetId="", creationTimestamp=time.time()),
            spec=payload.spec,
            status=TaskSetStatusModel(
                phase=PHASE_REJECTED,
                conditions=[{"type": "Rejected", "rejectedTaskTypes": rejected}],
            ),
        )
        return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())

    # 3) Cross-pod rate limit (per task_type, via indexed query)
    since = time.time() - _RATE_LIMIT_SECONDS
    in_cooldown: list[str] = []
    for t in allowed:
        if await _task_table.has_recent_task(t.type, since):
            in_cooldown.append(t.type)

    runnable = [t for t in allowed if t.type not in in_cooldown]

    if not runnable:
        resp = TaskSetResponse(
            metadata=TaskSetMetadata(tasksetId="", creationTimestamp=time.time()),
            spec=payload.spec,
            status=TaskSetStatusModel(
                phase=PHASE_RATE_LIMITED,
                conditions=[
                    {
                        "type": "RateLimited",
                        "rateLimitedTaskTypes": sorted(in_cooldown),
                        "cooldownSeconds": _RATE_LIMIT_SECONDS,
                        "rejectedTaskTypes": rejected,
                    }
                ],
            ),
        )
        return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())

    # 4) Persist task rows (one per runnable task_type, grouped by taskset_id)
    now = time.time()
    taskset_id = uuid.uuid4().hex
    pod_id = _pod_id()

    task_records = []
    for t in runnable:
        task_records.append(
            {
                "task_id": uuid.uuid4().hex,
                "taskset_id": taskset_id,
                "task_type": t.type,
                "target_workers": worker_ips,
                "creation_timestamp": now,
                "phase": PHASE_PENDING,
                "assigned_pod": pod_id,
            }
        )
    await _task_table.insert_tasks(task_records)

    asyncio.create_task(_run_tasks_async(taskset_id, runnable, worker_ips, task_records))

    audit_logger.info(
        f"create_taskset: taskset_id={taskset_id}, caller={caller}, "
        f"tasks={[t.type for t in runnable]}, workers={len(worker_ips)}, pod={pod_id}"
    )

    # Build response
    resp = _aggregate_taskset(taskset_id, task_records)
    result = resp.model_dump()
    if rejected or in_cooldown:
        result["conditions"] = [{"type": "Partial", "rejectedTaskTypes": rejected, "rateLimitedTaskTypes": in_cooldown}]
    return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=result)


@admin_ops_router.get("/tasksets/{taskset_id}")
@handle_exceptions(error_message="get taskset failed")
async def get_taskset(taskset_id: str) -> RockResponse[dict]:
    if _task_table is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="scheduler task table not initialised",
            error="server misconfigured",
        )

    tasks = await _task_table.get_tasks_by_group(taskset_id)
    if not tasks:
        resp = TaskSetResponse(
            metadata=TaskSetMetadata(tasksetId=taskset_id, creationTimestamp=0),
            spec=TaskSetSpec(),
            status=TaskSetStatusModel(phase=PHASE_NOT_FOUND),
        )
        return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())

    resp = _aggregate_taskset(taskset_id, tasks)
    return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())


# --- Background execution ---


async def _run_tasks_async(
    taskset_id: str,
    tasks: list[BaseTask],
    worker_ips: list[str],
    task_records: list[dict],
) -> None:
    """Execute each task and update its row."""
    for task, record in zip(tasks, task_records):
        tid = record["task_id"]
        await _task_table.update_task(tid, phase=PHASE_RUNNING, start_time=time.time())
        try:
            await task.run(worker_ips)
            result = [{"worker": ip, "success": True} for ip in worker_ips]
            await _task_table.update_task(tid, phase=PHASE_SUCCEEDED, completion_time=time.time(), result=result)
        except Exception as e:
            logger.exception(f"taskset '{taskset_id}' task '{task.type}' failed")
            result = [{"worker": ip, "success": False, "message": str(e)} for ip in worker_ips]
            conditions = [{"type": "Failed", "reason": "ExecutionError", "message": str(e)[:2048]}]
            await _task_table.update_task(
                tid, phase=PHASE_FAILED, completion_time=time.time(), result=result, conditions=conditions
            )

    audit_logger.info(f"taskset '{taskset_id}' done")
