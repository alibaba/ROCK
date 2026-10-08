"""SDK-facing endpoints: init_task_env, get_step, asample, close, training ops."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.config import get_settings
from tinker_backend.protocol.enums import ActionKind, ActionState, EnvironmentState, FutureState, RuntimeState
from tinker_backend.protocol.schemas import (
    CloseRuntimeRequest,
    CreateModelOutput,
    CreateModelRequest,
    CreateSamplingSessionOutput,
    CreateSamplingSessionRequest,
    ForwardBackwardRequest,
    ForwardRequest,
    FutureResponse,
    FutureRetrieveRequest,
    GetStepRequest,
    GetStepResponse,
    InitTaskEnvRequest,
    LoadWeightsRequest,
    OptimStepRequest,
    PublishToSamplerRequest,
    SampleRequest,
    SaveWeightsRequest,
)
from tinker_backend.server.deps import get_db_session
from tinker_backend.services.action_dispatcher import create_action_with_future, validate_runtime
from tinker_backend.services.environment_service import create_env, get_step_data
from tinker_backend.services.future_coordinator import check_close_actions_finished, get_future_by_id
from tinker_backend.services.runtime_lifecycle import (
    complete_close_runtime_future,
    create_close_runtime_future,
    create_completed_close_runtime_future,
    parse_runtime_config,
    terminate_runtime_processes,
)
from tinker_backend.services.training_registry import (
    create_model_record,
    create_sampling_session_record,
    load_weights_payload,
    prepare_publish_to_sampler,
    prepare_save_weights,
    require_model,
    resolve_sampling_payload,
)
from tinker_backend.storage.engine import get_sessionmaker
from tinker_backend.storage.models import (
    OperationFutureRecord,
    RuntimeActionRecord,
    RuntimeInstanceRecord,
    TaskEnvironmentRecord,
)

router = APIRouter()

RETRIEVE_FUTURE_TIMEOUT_SECONDS = 600.0
RETRIEVE_FUTURE_POLL_INTERVAL_SECONDS = 10.0
GET_STEP_TIMEOUT_SECONDS = 600.0
GET_STEP_POLL_INTERVAL_SECONDS = 1.0
CLOSE_ACTION_TYPES = (
    ActionKind.CLOSE_RUNTIME.value,
    ActionKind.CLOSE_ROCK_ADAPTER.value,
)


def _runtime_uses_rock(runtime: RuntimeInstanceRecord) -> bool:
    try:
        config = parse_runtime_config(runtime.config_content)
    except Exception:
        return False
    rock = config.get("rock")
    if not isinstance(rock, dict):
        return False
    return bool(rock.get("enabled", True))


async def _validate_ready_runtime(session: AsyncSession, runtime_id: str) -> RuntimeInstanceRecord:
    try:
        runtime = await validate_runtime(session, runtime_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not runtime.ready or runtime.status != RuntimeState.READY.value:
        raise HTTPException(status_code=409, detail=f"Runtime {runtime_id} is not ready")
    return runtime


def _env_failure_detail(env: TaskEnvironmentRecord) -> str:
    metadata = env.env_metadata if isinstance(env.env_metadata, dict) else {}
    message = metadata.get("error_message") or "Task environment failed"
    action_type = metadata.get("failed_action_type")
    action_id = metadata.get("failed_action_id")
    if action_type or action_id:
        return f"{message} (failed_action_type={action_type}, failed_action_id={action_id})"
    return str(message)


async def _wait_for_step_data(runtime_id: str, env_id: str, step_id: int, req: Request) -> GetStepResponse:
    deadline = time.perf_counter() + GET_STEP_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        if await req.is_disconnected():
            raise HTTPException(status_code=499, detail="Client disconnected")
        async with get_sessionmaker()() as session:
            env = await session.get(TaskEnvironmentRecord, env_id)
            if env is None:
                raise HTTPException(status_code=404, detail="Env not found")
            if env.runtime_id != runtime_id:
                raise HTTPException(status_code=403, detail="Env does not belong to this runtime")
            step = await get_step_data(session, env_id, step_id)
            warning_message = _env_failure_detail(env) if env.status == EnvironmentState.FAILED.value else None
            if step is not None and step.finish_reason is not None:
                return GetStepResponse(
                    step_id=step.step_id,
                    prompt=step.prompt,
                    finish_reason=step.finish_reason,
                    reward=step.reward,
                    warning_message=warning_message,
                )
            if env.status == EnvironmentState.FAILED.value:
                raise HTTPException(status_code=400, detail=_env_failure_detail(env))
        if step is not None:
            return GetStepResponse(
                step_id=step.step_id,
                prompt=step.prompt,
                finish_reason=step.finish_reason,
                reward=step.reward,
                warning_message=None,
            )
        await asyncio.sleep(min(GET_STEP_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.perf_counter())))
    raise HTTPException(status_code=408, detail="Timeout waiting for step data", headers={"X-Tinker-Queue-State": "active"})


async def _enforce_runtime_close(runtime_id: str, future_id: int, force_after_seconds: float) -> None:
    deadline = time.perf_counter() + max(0.0, force_after_seconds)
    graceful = False
    action_error: str | None = None

    while time.perf_counter() < deadline:
        async with get_sessionmaker()() as session:
            graceful, action_error = await check_close_actions_finished(runtime_id, future_id, session)
        if graceful:
            break
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.perf_counter())))

    async with get_sessionmaker()() as session:
        runtime = await session.get(RuntimeInstanceRecord, runtime_id)
        if runtime is None:
            return
        killed_pids = await asyncio.to_thread(terminate_runtime_processes, runtime)
        if graceful and action_error is None:
            message = "runtime closed gracefully; residual processes cleaned"
        elif action_error:
            message = f"runtime close action failed; forced cleanup: {action_error}"
        else:
            message = "runtime close action timed out; forced cleanup"
        await complete_close_runtime_future(
            session, runtime, future_id,
            graceful=graceful and action_error is None,
            killed_pids=killed_pids,
            message=message,
        )
        await session.commit()


@router.post("/api/v1/retrieve_future")
async def retrieve_future(request: FutureRetrieveRequest, req: Request) -> dict[str, Any]:
    try:
        request_id = int(request.request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request_id must be an integer") from exc

    deadline = time.perf_counter() + RETRIEVE_FUTURE_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        if await req.is_disconnected():
            raise HTTPException(status_code=499, detail="Client disconnected")
        async with get_sessionmaker()() as session:
            future = await get_future_by_id(session, request_id)
        if future is None:
            raise HTTPException(status_code=404, detail="Future not found")
        if future.status == FutureState.COMPLETED.value:
            return future.result_data or {}
        if future.status == FutureState.FAILED.value:
            detail = future.error_message or "Future failed"
            raise HTTPException(status_code=400, detail=detail)
        await asyncio.sleep(min(RETRIEVE_FUTURE_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.perf_counter())))
    raise HTTPException(status_code=408, detail="Timeout waiting for result", headers={"X-Tinker-Queue-State": "active"})


@router.post("/api/v1/sdk/{runtime_id}/close", response_model=FutureResponse)
async def close_runtime(
    runtime_id: str,
    body: CloseRuntimeRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> FutureResponse:
    runtime = await session.get(RuntimeInstanceRecord, runtime_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Runtime not found")
    force_after_seconds = 30.0 if body is None or body.force_after_seconds is None else float(body.force_after_seconds)
    if runtime.status == RuntimeState.STOPPED.value and not runtime.ready:
        future = await create_completed_close_runtime_future(session, runtime, message="runtime already stopped")
        await session.commit()
        return FutureResponse(type="close_runtime", future_id=str(future.request_id), request_id=str(future.request_id), status=future.status, runtime_id=runtime_id)
    _action, future = await create_close_runtime_future(session, runtime, force_after_seconds=force_after_seconds)
    await session.commit()
    asyncio.create_task(_enforce_runtime_close(runtime_id, future.request_id, force_after_seconds))
    return FutureResponse(type="close_runtime", future_id=str(future.request_id), request_id=str(future.request_id), status=future.status, runtime_id=runtime_id)


@router.post("/api/v1/sdk/{runtime_id}/init_task_env", response_model=FutureResponse)
async def init_task_env(
    runtime_id: str,
    body: InitTaskEnvRequest,
    session: AsyncSession = Depends(get_db_session),
) -> FutureResponse:
    runtime = await _validate_ready_runtime(session, runtime_id)
    metadata = dict(body.metadata or {})
    if _runtime_uses_rock(runtime):
        metadata["rock_managed"] = True
    env = await create_env(session, runtime_id=runtime_id, task_id=body.task_id, dataset=body.dataset, split=body.split, env_metadata=metadata)
    payload = {"env_id": env.env_id, "task_id": body.task_id, "dataset": body.dataset, "split": body.split, "metadata": metadata}
    _action, future = await create_action_with_future(session, runtime_id=runtime_id, action_type="init_task_env", payload=payload, env_id=env.env_id, request_type="init_task_env")
    await session.commit()
    return FutureResponse(type="init_task_env", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id)


@router.post("/api/v1/sdk/{runtime_id}/get_step", response_model=GetStepResponse)
async def get_step_endpoint(runtime_id: str, body: GetStepRequest, req: Request) -> GetStepResponse:
    step_id = 0 if body.step_id is None else body.step_id
    return await _wait_for_step_data(runtime_id, body.env_id, step_id, req)


@router.post("/api/v1/sdk/{runtime_id}/asample", response_model=FutureResponse)
async def asample(runtime_id: str, body: SampleRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        payload = await resolve_sampling_payload(
            session,
            runtime_id=runtime_id,
            payload=body.model_dump(mode="json"),
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    model_id = payload.get("model_id")
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="sample",
        payload=payload,
        env_id=body.env_id,
        request_type="sample",
        model_id=str(model_id) if model_id else None,
    )
    await session.commit()
    return FutureResponse(type="sample", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id)


@router.post("/api/v1/sdk/{runtime_id}/create_model", response_model=FutureResponse)
async def create_model(runtime_id: str, body: CreateModelRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        model = await create_model_record(session, runtime_id=runtime_id, request=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = {
        "model_id": model.model_id,
        "base_model": model.base_model,
        "lora_config": model.lora_config,
        "session_id": model.session_id,
        "model_seq_id": model.model_seq_id,
        "user_metadata": model.user_metadata,
    }
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="create_model",
        payload=payload,
        request_type="create_model",
        model_id=model.model_id,
    )
    model.create_future_id = future.request_id
    await session.commit()
    return FutureResponse(
        type="create_model",
        future_id=str(future.request_id),
        request_id=str(future.request_id),
        status="pending",
        runtime_id=runtime_id,
        model_id=model.model_id,
    )


@router.post("/api/v1/sdk/{runtime_id}/create_sampling_session", response_model=CreateSamplingSessionOutput)
async def create_sampling_session(
    runtime_id: str,
    body: CreateSamplingSessionRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CreateSamplingSessionOutput:
    await _validate_ready_runtime(session, runtime_id)
    try:
        sampling = await create_sampling_session_record(session, runtime_id=runtime_id, request=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return CreateSamplingSessionOutput(sampling_session_id=sampling.sampling_session_id)


@router.post("/api/v1/sdk/{runtime_id}/forward_backward", response_model=FutureResponse)
async def forward_backward(runtime_id: str, body: ForwardBackwardRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        await require_model(session, runtime_id=runtime_id, model_id=body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = body.model_dump(mode="json")
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="forward_backward",
        payload=payload,
        request_type="forward_backward",
        model_id=body.model_id,
    )
    await session.commit()
    return FutureResponse(type="forward_backward", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)


@router.post("/api/v1/sdk/{runtime_id}/forward", response_model=FutureResponse)
async def forward(runtime_id: str, body: ForwardRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        await require_model(session, runtime_id=runtime_id, model_id=body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = body.model_dump(mode="json")
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="forward",
        payload=payload,
        request_type="forward",
        model_id=body.model_id,
    )
    await session.commit()
    return FutureResponse(type="forward", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)


@router.post("/api/v1/sdk/{runtime_id}/optim_step", response_model=FutureResponse)
async def optim_step(runtime_id: str, body: OptimStepRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        await require_model(session, runtime_id=runtime_id, model_id=body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = body.model_dump(mode="json")
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="optim_step",
        payload=payload,
        request_type="optim_step",
        model_id=body.model_id,
    )
    await session.commit()
    return FutureResponse(type="optim_step", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)


@router.post("/api/v1/sdk/{runtime_id}/save_weights", response_model=FutureResponse)
async def save_weights(runtime_id: str, body: SaveWeightsRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        checkpoint = await prepare_save_weights(session, runtime_id=runtime_id, request=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = {**body.model_dump(mode="json"), "checkpoint_id": checkpoint.checkpoint_id}
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="save_weights",
        payload=payload,
        request_type="save_weights",
        model_id=body.model_id,
    )
    checkpoint.future_id = future.request_id
    await session.commit()
    return FutureResponse(type="save_weights", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)


@router.post("/api/v1/sdk/{runtime_id}/publish_to_sampler", response_model=FutureResponse)
async def publish_to_sampler(runtime_id: str, body: PublishToSamplerRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        checkpoint, sampling = await prepare_publish_to_sampler(session, runtime_id=runtime_id, request=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = {
        **body.model_dump(mode="json"),
        "checkpoint_id": checkpoint.checkpoint_id,
        "sampling_session_id": sampling.sampling_session_id if sampling is not None else None,
    }
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="publish_to_sampler",
        payload=payload,
        request_type="publish_to_sampler",
        model_id=body.model_id,
    )
    checkpoint.future_id = future.request_id
    await session.commit()
    return FutureResponse(type="publish_to_sampler", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)


@router.post("/api/v1/sdk/{runtime_id}/load_weights", response_model=FutureResponse)
async def load_weights(runtime_id: str, body: LoadWeightsRequest, session: AsyncSession = Depends(get_db_session)) -> FutureResponse:
    await _validate_ready_runtime(session, runtime_id)
    try:
        await require_model(session, runtime_id=runtime_id, model_id=body.model_id)
        payload = load_weights_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _action, future = await create_action_with_future(
        session,
        runtime_id=runtime_id,
        action_type="load_weights",
        payload=payload,
        request_type="load_weights",
        model_id=body.model_id,
    )
    await session.commit()
    return FutureResponse(type="load_weights", future_id=str(future.request_id), request_id=str(future.request_id), status="pending", runtime_id=runtime_id, model_id=body.model_id)
