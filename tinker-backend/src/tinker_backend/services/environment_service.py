"""Task environment and step management."""

from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import EnvironmentState
from tinker_backend.storage.models import (
    EnvironmentStepRecord,
    TaskEnvironmentRecord,
    utcnow,
)


def make_env_id() -> str:
    return "env_" + uuid4().hex[:12]


async def create_env(
    session: AsyncSession,
    runtime_id: str,
    task_id: str,
    dataset: str,
    split: str,
    env_metadata: dict[str, Any] | None = None,
) -> TaskEnvironmentRecord:
    env_id = make_env_id()
    env = TaskEnvironmentRecord(
        env_id=env_id,
        runtime_id=runtime_id,
        task_id=task_id,
        dataset=dataset,
        split=split,
        env_metadata=env_metadata or {},
    )
    session.add(env)
    await session.flush()
    return env


async def post_step(
    session: AsyncSession,
    env_id: str,
    step_id: int,
    prompt: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    reward: float | None = None,
) -> EnvironmentStepRecord:
    existing = await session.get(EnvironmentStepRecord, (env_id, step_id))
    if existing is not None:
        existing.prompt = prompt
        existing.finish_reason = finish_reason
        existing.reward = reward
        await _update_env_from_step(session, env_id, finish_reason)
        await session.flush()
        return existing

    step = EnvironmentStepRecord(
        env_id=env_id,
        step_id=step_id,
        prompt=prompt,
        finish_reason=finish_reason,
        reward=reward,
    )
    session.add(step)
    await _update_env_from_step(session, env_id, finish_reason)
    await session.flush()
    return step


async def _update_env_from_step(session: AsyncSession, env_id: str, finish_reason: str | None) -> None:
    env = await session.get(TaskEnvironmentRecord, env_id)
    if env is None:
        return
    env.updated_at = utcnow()
    if finish_reason is not None:
        env.status = EnvironmentState.DONE.value
    elif env.status == EnvironmentState.PENDING_INIT.value:
        env.status = EnvironmentState.ACTIVE.value


async def get_step_data(
    session: AsyncSession,
    env_id: str,
    step_id: int,
) -> EnvironmentStepRecord | None:
    return await session.get(EnvironmentStepRecord, (env_id, step_id))
