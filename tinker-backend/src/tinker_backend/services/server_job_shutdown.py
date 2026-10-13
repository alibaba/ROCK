"""ServerJob-level backend shutdown helpers."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import RuntimeState
from tinker_backend.services.future_coordinator import check_close_actions_finished
from tinker_backend.services.runtime_lifecycle import (
    complete_close_runtime_future,
    create_close_runtime_future,
    terminate_runtime_processes,
)
from tinker_backend.storage.models import RuntimeInstanceRecord


async def _close_runtime_for_server_job(
    session: AsyncSession,
    runtime: RuntimeInstanceRecord,
    *,
    force_after_seconds: float,
) -> dict[str, Any]:
    if runtime.status == RuntimeState.STOPPED.value and not runtime.ready:
        return {"runtime_id": runtime.runtime_id, "status": "already_stopped", "killed_pids": []}

    _action, future = await create_close_runtime_future(
        session,
        runtime,
        force_after_seconds=force_after_seconds,
    )
    await session.commit()

    deadline = time.perf_counter() + max(0.0, force_after_seconds)
    graceful = False
    action_error: str | None = None
    while time.perf_counter() < deadline:
        graceful, action_error = await check_close_actions_finished(runtime.runtime_id, future.request_id, session)
        if graceful:
            break
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.perf_counter())))

    await session.refresh(runtime)
    killed_pids = await asyncio.to_thread(terminate_runtime_processes, runtime)
    if graceful and action_error is None:
        message = "runtime closed gracefully during server job stop; residual processes cleaned"
    elif action_error:
        message = f"runtime close action failed during server job stop; forced cleanup: {action_error}"
    else:
        message = "runtime close action timed out during server job stop; forced cleanup"

    await complete_close_runtime_future(
        session,
        runtime,
        future.request_id,
        graceful=graceful and action_error is None,
        killed_pids=killed_pids,
        message=message,
    )
    await session.commit()
    return {
        "runtime_id": runtime.runtime_id,
        "status": "stopped",
        "graceful": graceful and action_error is None,
        "killed_pids": killed_pids,
        "message": message,
    }


async def stop_all_runtimes_for_server_job(
    session: AsyncSession,
    *,
    force_after_seconds: float = 30.0,
) -> dict[str, Any]:
    result = await session.execute(
        select(RuntimeInstanceRecord).where(RuntimeInstanceRecord.status != RuntimeState.STOPPED.value)
    )
    runtimes = list(result.scalars().all())
    closed: list[dict[str, Any]] = []
    errors: list[str] = []
    for runtime in runtimes:
        try:
            closed.append(
                await _close_runtime_for_server_job(
                    session,
                    runtime,
                    force_after_seconds=force_after_seconds,
                )
            )
        except Exception as exc:  # noqa: BLE001 - shutdown must continue best-effort.
            errors.append(f"{runtime.runtime_id}: {exc}")
    return {
        "runtime_count": len(runtimes),
        "closed_runtime_ids": [item["runtime_id"] for item in closed],
        "closed_runtimes": closed,
        "errors": errors,
    }
