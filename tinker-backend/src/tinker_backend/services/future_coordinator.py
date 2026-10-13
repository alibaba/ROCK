"""Future resolution and close-action tracking."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import ActionKind, ActionState, FutureState
from tinker_backend.storage.models import (
    OperationFutureRecord,
    RuntimeActionRecord,
    utcnow,
)

CLOSE_ACTION_TYPES = (
    ActionKind.CLOSE_RUNTIME.value,
    ActionKind.CLOSE_ROCK_ADAPTER.value,
)


async def check_close_actions_finished(runtime_id: str, future_id: int, session: AsyncSession) -> tuple[bool, str | None]:
    result = await session.execute(
        select(RuntimeActionRecord)
        .where(RuntimeActionRecord.runtime_id == runtime_id)
        .where(RuntimeActionRecord.future_id == future_id)
        .where(RuntimeActionRecord.action_type.in_(CLOSE_ACTION_TYPES))
        .order_by(RuntimeActionRecord.action_id)
    )
    actions = result.scalars().all()
    if not actions:
        return False, None
    failed_messages: list[str] = []
    for action in actions:
        if action.status not in {ActionState.COMPLETED.value, ActionState.FAILED.value}:
            return False, None
        if action.status == ActionState.FAILED.value:
            failed_messages.append(
                f"{action.action_type}: {action.error_message or 'close action failed'}"
            )
    if failed_messages:
        return True, "; ".join(failed_messages)
    return True, None


async def get_future_by_id(session: AsyncSession, request_id: int) -> OperationFutureRecord | None:
    result = await session.execute(
        select(OperationFutureRecord).where(OperationFutureRecord.request_id == request_id)
    )
    return result.scalar_one_or_none()
