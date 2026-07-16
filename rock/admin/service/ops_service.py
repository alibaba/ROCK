"""OpsService: business logic for admin ops TaskSet lifecycle."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Callable

from rock.admin.core.scheduler_task_table import Phase, SchedulerTaskTable
from rock.admin.core.schema import SchedulerTaskRecord
from rock.admin.proto.request import TaskSetSpec
from rock.admin.proto.response import (
    TaskMetadata,
    TaskResponse,
    TaskSetMetadata,
    TaskSetResponse,
    TaskSetStatusModel,
    TaskStatusModel,
)
from rock.admin.scheduler.task_base import BaseTask, TaskRunOutcome
from rock.logger import init_logger

logger = init_logger(__name__)
audit_logger = init_logger("admin_ops_audit")

_RATE_LIMIT_SECONDS = 60
_WHITELIST_SUFFIXES = ("_cleanup", "_prune", "_archive")


class OpsService:
    def __init__(
        self,
        task_table: SchedulerTaskTable,
        task_registry: dict[str, BaseTask],
        alive_workers_provider: Callable[[], set[str]],
    ) -> None:
        self._task_table = task_table
        self._task_registry = task_registry
        self._alive_workers_provider = alive_workers_provider

    async def create_taskset(self, spec: TaskSetSpec, caller: str) -> TaskSetResponse:
        audit_logger.info(f"create_taskset: caller={caller}, spec={spec.model_dump()}")

        worker_ips = self._resolve_workers(spec)
        allowed, rejected = self._resolve_tasks(spec.taskTypes)

        if not allowed:
            return TaskSetResponse(
                metadata=TaskSetMetadata(tasksetId="", creationTimestamp=time.time()),
                spec=spec,
                status=TaskSetStatusModel(
                    phase=Phase.REJECTED,
                    conditions=[{"type": "Rejected", "rejectedTaskTypes": rejected}],
                ),
            )

        since = time.time() - _RATE_LIMIT_SECONDS
        in_cooldown: list[str] = []
        for t in allowed:
            if await self._task_table.has_recent_task(t.type, since):
                in_cooldown.append(t.type)

        runnable = [t for t in allowed if t.type not in in_cooldown]

        if not runnable:
            return TaskSetResponse(
                metadata=TaskSetMetadata(tasksetId="", creationTimestamp=time.time()),
                spec=spec,
                status=TaskSetStatusModel(
                    phase=Phase.RATE_LIMITED,
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

        now = time.time()
        taskset_id = uuid.uuid4().hex
        pod_id = _pod_id()

        records = [
            SchedulerTaskRecord(
                task_id=uuid.uuid4().hex,
                taskset_id=taskset_id,
                task_type=t.type,
                target_workers=worker_ips,
                creation_timestamp=now,
                phase=Phase.PENDING,
                assigned_pod=pod_id,
            )
            for t in runnable
        ]
        await self._task_table.insert_tasks(records)

        asyncio.create_task(self._run_tasks_async(taskset_id, runnable, worker_ips, records))

        audit_logger.info(
            f"create_taskset: taskset_id={taskset_id}, caller={caller}, "
            f"tasks={[t.type for t in runnable]}, workers={len(worker_ips)}, pod={pod_id}"
        )

        resp = _aggregate_taskset(taskset_id, records)
        if rejected or in_cooldown:
            resp.status.conditions = [
                {"type": "Partial", "rejectedTaskTypes": rejected, "rateLimitedTaskTypes": in_cooldown}
            ]
        return resp

    async def get_taskset(self, taskset_id: str) -> TaskSetResponse:
        tasks = await self._task_table.get_tasks_by_group(taskset_id)
        if not tasks:
            return TaskSetResponse(
                metadata=TaskSetMetadata(tasksetId=taskset_id, creationTimestamp=0),
                spec=TaskSetSpec(),
                status=TaskSetStatusModel(phase=Phase.NOT_FOUND),
            )
        return _aggregate_taskset(taskset_id, tasks)

    def _resolve_workers(self, spec: TaskSetSpec) -> list[str]:
        if spec.targetWorkers is not None:
            return list(spec.targetWorkers)
        try:
            return list(self._alive_workers_provider())
        except Exception as e:
            logger.warning(f"alive workers provider failed: {e}")
            return []

    def _resolve_tasks(self, requested: list[str] | None) -> tuple[list[BaseTask], list[str]]:
        if requested is None:
            return [t for name, t in self._task_registry.items() if _is_whitelisted(name)], []
        allowed: list[BaseTask] = []
        rejected: list[str] = []
        for name in requested:
            if not _is_whitelisted(name):
                rejected.append(name)
                continue
            if name not in self._task_registry:
                rejected.append(name)
                continue
            allowed.append(self._task_registry[name])
        return allowed, rejected

    async def _run_tasks_async(
        self,
        taskset_id: str,
        tasks: list[BaseTask],
        worker_ips: list[str],
        records: list[SchedulerTaskRecord],
    ) -> None:
        worker_ip_set = set(worker_ips)
        for task, record in zip(tasks, records):
            tid = record.task_id
            await self._task_table.update_task(tid, phase=Phase.RUNNING, start_time=time.time())
            try:
                report = await task.run(worker_ip_set)
                status = []
                for worker_result in report.worker_results:
                    worker_status = {
                        "worker": worker_result.worker_ip,
                        "success": worker_result.succeeded,
                    }
                    if worker_result.error:
                        worker_status["message"] = worker_result.error
                    status.append(worker_status)

                update_fields = {
                    "phase": Phase.SUCCEEDED,
                    "completion_time": time.time(),
                    "status": status,
                }
                if report.failed_count:
                    condition_type = "PartialFailure" if report.outcome == TaskRunOutcome.PARTIAL else "Failed"
                    update_fields["phase"] = Phase.FAILED
                    update_fields["conditions"] = [
                        {
                            "type": condition_type,
                            "reason": report.outcome.value,
                            "message": (f"{report.failed_count}/{report.total_count} workers failed or timed out"),
                        }
                    ]
                await self._task_table.update_task(tid, **update_fields)
            except Exception as e:
                logger.exception(f"taskset '{taskset_id}' task '{task.type}' failed")
                status = [{"worker": ip, "success": False, "message": str(e)} for ip in worker_ips]
                conditions = [{"type": "Failed", "reason": "ExecutionError", "message": str(e)[:2048]}]
                await self._task_table.update_task(
                    tid, phase=Phase.FAILED, completion_time=time.time(), status=status, conditions=conditions
                )

        audit_logger.info(f"taskset '{taskset_id}' done")


def _is_whitelisted(task_type: str) -> bool:
    return any(task_type.endswith(s) for s in _WHITELIST_SUFFIXES)


def _pod_id() -> str:
    return os.environ.get("HOSTNAME") or "unknown"


def _aggregate_taskset(taskset_id: str, tasks: list[SchedulerTaskRecord]) -> TaskSetResponse:
    task_responses = [
        TaskResponse(
            metadata=TaskMetadata(
                taskId=t.task_id,
                tasksetId=t.taskset_id,
                creationTimestamp=t.creation_timestamp,
            ),
            spec={"taskType": t.task_type, "targetWorkers": t.target_workers},
            status=TaskStatusModel(
                phase=t.phase,
                startTime=t.start_time,
                completionTime=t.completion_time,
                conditions=t.conditions,
                status=t.status,
            ),
        )
        for t in tasks
    ]

    succeeded = sum(1 for t in tasks if t.phase == Phase.SUCCEEDED)
    failed = sum(1 for t in tasks if t.phase == Phase.FAILED)
    active = len(tasks) - succeeded - failed

    if active > 0:
        phase = Phase.RUNNING
    elif failed > 0:
        phase = Phase.FAILED
    else:
        phase = Phase.SUCCEEDED

    start_times = [t.start_time for t in tasks if t.start_time]
    completion_times = [t.completion_time for t in tasks if t.completion_time]

    return TaskSetResponse(
        metadata=TaskSetMetadata(
            tasksetId=taskset_id,
            creationTimestamp=min(t.creation_timestamp for t in tasks),
        ),
        spec=TaskSetSpec(
            targetWorkers=tasks[0].target_workers if tasks else None,
        ),
        status=TaskSetStatusModel(
            phase=phase,
            assignedPod=tasks[0].assigned_pod if tasks else "",
            active=active,
            succeeded=succeeded,
            failed=failed,
            startTime=min(start_times) if start_times else None,
            completionTime=max(completion_times) if completion_times and active == 0 else None,
        ),
        tasks=task_responses,
    )
