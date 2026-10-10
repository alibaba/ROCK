"""Action lifecycle: create, claim, complete, and auto-enqueue deliver_sample."""

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import ActionKind, ActionState, EnvironmentState, FutureState, RuntimeState
from tinker_backend.storage.models import (
    OperationFutureRecord,
    RuntimeActionRecord,
    RuntimeInstanceRecord,
    TaskEnvironmentRecord,
    utcnow,
)
from tinker_backend.services.training_registry import mark_action_records, normalize_action_result

# Long ROCK actions include sandbox setup, ModelService install/start, and
# anti_call_llm waits. Keep the lease longer than anti_call_timeout_sec so a
# healthy but slow action is not claimed by a second adapter task.
CLAIM_LEASE_SECONDS = 3600
DELIVER_SAMPLE_ACTION_TYPE = "deliver_sample"
CLOSE_ACTION_TYPES = {
    ActionKind.CLOSE_RUNTIME.value,
    ActionKind.CLOSE_ROCK_ADAPTER.value,
}
RUNTIME_CLOSE_ACTION_TYPES = {ActionKind.CLOSE_RUNTIME.value, ActionKind.CLOSE_ROCK_ADAPTER.value}


async def create_action_with_future(
    session: AsyncSession,
    runtime_id: str,
    action_type: str,
    payload: dict[str, Any],
    env_id: str | None = None,
    request_type: str | None = None,
    model_id: str | None = None,
) -> tuple[RuntimeActionRecord, OperationFutureRecord]:
    future = OperationFutureRecord(
        request_type=request_type or action_type,
        runtime_id=runtime_id,
        model_id=model_id,
        request_data=payload,
        status=FutureState.PENDING.value,
    )
    session.add(future)
    await session.flush()

    action = RuntimeActionRecord(
        runtime_id=runtime_id,
        action_type=action_type,
        env_id=env_id,
        future_id=future.request_id,
        payload=payload,
    )
    session.add(action)
    await session.flush()
    return action, future


async def claim_actions(
    session: AsyncSession,
    runtime_id: str,
    action_types: list[str] | None = None,
    limit: int = 10,
) -> list[RuntimeActionRecord]:
    now = utcnow()
    stale_claim_cutoff = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    query = (
        select(RuntimeActionRecord)
        .where(RuntimeActionRecord.runtime_id == runtime_id)
        .where(
            or_(
                RuntimeActionRecord.status == ActionState.PENDING.value,
                (RuntimeActionRecord.status == ActionState.CLAIMED.value)
                & (RuntimeActionRecord.claimed_at < stale_claim_cutoff),
            )
        )
        .order_by(RuntimeActionRecord.action_id)
        .limit(limit)
    )
    if action_types:
        query = query.where(RuntimeActionRecord.action_type.in_(action_types))

    result = await session.execute(query)
    actions = list(result.scalars().all())

    for action in actions:
        action.status = ActionState.CLAIMED.value
        action.claimed_at = now

    await session.flush()
    return actions


async def post_action_result(
    session: AsyncSession,
    runtime_id: str,
    action_id: int,
    status: str,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> RuntimeActionRecord:
    action = await session.get(RuntimeActionRecord, action_id)
    if action is None:
        raise ValueError(f"Action {action_id} not found")
    if action.runtime_id != runtime_id:
        raise ValueError(f"Action {action_id} does not belong to runtime {runtime_id}")
    if action.status != ActionState.CLAIMED.value:
        raise ValueError(f"Action {action_id} is not in claimed state (current: {action.status})")

    if status == ActionState.COMPLETED.value:
        result_data = normalize_action_result(action, result_data)

    action.status = status
    action.result_data = result_data
    action.error_message = error_message
    action.completed_at = utcnow()

    is_close_action = action.action_type in CLOSE_ACTION_TYPES
    should_complete_directly = not is_close_action
    future = None

    if action.future_id is not None and should_complete_directly:
        future = await session.get(OperationFutureRecord, action.future_id)
        if future is not None and future.status == FutureState.PENDING.value:
            if status == ActionState.COMPLETED.value:
                future.status = FutureState.COMPLETED.value
                future.result_data = result_data
            else:
                future.status = FutureState.FAILED.value
                future.error_message = error_message
            future.completed_at = utcnow()

    if action.env_id and status == ActionState.FAILED.value:
        await _mark_env_failed(session, action, error_message)

    if action.action_type in RUNTIME_CLOSE_ACTION_TYPES:
        runtime = await session.get(RuntimeInstanceRecord, action.runtime_id)
        if runtime is not None:
            runtime.status = RuntimeState.STOPPING.value
            runtime.ready = False
            runtime.updated_at = utcnow()
    elif status == ActionState.COMPLETED.value:
        await _maybe_enqueue_deliver_sample(session, action, result_data)

    await mark_action_records(
        session,
        action=action,
        status=status,
        result_data=result_data,
        error_message=error_message,
    )

    await session.flush()
    return action


async def _mark_env_failed(
    session: AsyncSession,
    action: RuntimeActionRecord,
    error_message: str | None,
) -> None:
    if not action.env_id:
        return
    env = await session.get(TaskEnvironmentRecord, action.env_id)
    if env is None:
        return
    env.updated_at = utcnow()
    metadata = dict(env.env_metadata or {})
    failure_message = error_message or f"{action.action_type} failed"
    if env.status == EnvironmentState.DONE.value:
        metadata["warning_message"] = failure_message
        metadata["warning_action_id"] = action.action_id
        metadata["warning_action_type"] = action.action_type
        env.env_metadata = metadata
        return
    env.status = EnvironmentState.FAILED.value
    metadata["error_message"] = failure_message
    metadata["failed_action_id"] = action.action_id
    metadata["failed_action_type"] = action.action_type
    env.env_metadata = metadata


async def _maybe_enqueue_deliver_sample(
    session: AsyncSession,
    action: RuntimeActionRecord,
    result_data: dict[str, Any] | None,
) -> None:
    if action.action_type != ActionKind.SAMPLE.value:
        return
    if not action.env_id or result_data is None:
        return

    env = await session.get(TaskEnvironmentRecord, action.env_id)
    if env is None or not (env.env_metadata or {}).get("rock_managed"):
        return

    session.add(
        RuntimeActionRecord(
            runtime_id=action.runtime_id,
            action_type=DELIVER_SAMPLE_ACTION_TYPE,
            env_id=action.env_id,
            future_id=None,
            payload={
                "sample_action_id": action.action_id,
                "sample_response": result_data,
            },
        )
    )


async def validate_runtime(
    session: AsyncSession,
    runtime_id: str,
) -> RuntimeInstanceRecord:
    runtime = await session.get(RuntimeInstanceRecord, runtime_id)
    if runtime is None:
        raise ValueError(f"Runtime {runtime_id} not found")
    return runtime
