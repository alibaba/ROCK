"""Adapter-facing endpoints: action claim, result, and step posting."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.schemas import (
    ClaimActionsRequest,
    ClaimActionsResponse,
    ClaimedAction,
    PostActionResultRequest,
    PostActionResultResponse,
    PostStepRequest,
    PostStepResponse,
)
from tinker_backend.server.deps import get_db_session
from tinker_backend.services.action_dispatcher import (
    claim_actions,
    post_action_result,
    validate_runtime,
)
from tinker_backend.services.environment_service import post_step
from tinker_backend.storage.models import TaskEnvironmentRecord

router = APIRouter()


@router.post("/api/v1/runtimes/{runtime_id}/actions/claim", response_model=ClaimActionsResponse)
async def claim_runtime_actions(
    runtime_id: str,
    body: ClaimActionsRequest,
    session: AsyncSession = Depends(get_db_session),
) -> ClaimActionsResponse:
    try:
        await validate_runtime(session, runtime_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    actions = await claim_actions(
        session,
        runtime_id=runtime_id,
        action_types=body.action_types,
        limit=body.limit,
    )
    await session.commit()
    return ClaimActionsResponse(
        actions=[
            ClaimedAction(
                action_id=a.action_id,
                action_type=a.action_type,
                env_id=a.env_id,
                payload=a.payload,
            )
            for a in actions
        ]
    )


@router.post(
    "/api/v1/runtimes/{runtime_id}/actions/{action_id}/result",
    response_model=PostActionResultResponse,
)
async def post_runtime_action_result(
    runtime_id: str,
    action_id: int,
    body: PostActionResultRequest,
    session: AsyncSession = Depends(get_db_session),
) -> PostActionResultResponse:
    try:
        await post_action_result(
            session,
            runtime_id=runtime_id,
            action_id=action_id,
            status=body.status,
            result_data=body.result_data,
            error_message=body.error_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return PostActionResultResponse()


@router.post(
    "/api/v1/runtimes/{runtime_id}/envs/{env_id}/steps",
    response_model=PostStepResponse,
)
async def post_runtime_step(
    runtime_id: str,
    env_id: str,
    body: PostStepRequest,
    session: AsyncSession = Depends(get_db_session),
) -> PostStepResponse:
    env = await session.get(TaskEnvironmentRecord, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="Env not found")
    if env.runtime_id != runtime_id:
        raise HTTPException(status_code=403, detail="Env does not belong to this runtime")
    await post_step(
        session,
        env_id=env_id,
        step_id=body.step_id,
        prompt=body.prompt.model_dump(mode="json") if body.prompt is not None else None,
        finish_reason=body.finish_reason,
        reward=body.reward,
    )
    await session.commit()
    return PostStepResponse()
