"""Runtime CRUD and heartbeat endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.config import get_settings
from tinker_backend.protocol.enums import RuntimeState
from tinker_backend.protocol.schemas import (
    CreateRuntimeRequest,
    FutureResponse,
    RuntimeHeartbeatRequest,
    RuntimeHeartbeatResponse,
    RuntimeStatusResponse,
)
from tinker_backend.server.deps import get_db_session
from tinker_backend.services.runtime_lifecycle import (
    create_runtime_future,
    launch_runtime_process,
    record_runtime_heartbeat,
)
from tinker_backend.storage.models import (
    ClientSessionRecord,
    RuntimeInstanceRecord,
)

router = APIRouter()


def _runtime_status(runtime: RuntimeInstanceRecord) -> RuntimeStatusResponse:
    return RuntimeStatusResponse(
        runtime_id=runtime.runtime_id,
        runtime_type=runtime.runtime_type,
        status=runtime.status,
        ready=runtime.ready,
        config_type=runtime.config_type,
        config_path=runtime.config_path,
        adapter_base_url=runtime.adapter_base_url,
        error_message=runtime.error_message,
        session_id=runtime.session_id,
    )


@router.post("/api/v1/runtimes", response_model=FutureResponse)
@router.post("/api/v1/create_runtime", response_model=FutureResponse)
async def create_runtime(
    request: CreateRuntimeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> FutureResponse:
    if request.session_id is not None and await session.get(ClientSessionRecord, request.session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")

    runtime, future = await create_runtime_future(session, get_settings(), request)
    await session.commit()

    await launch_runtime_process(session, get_settings(), runtime)
    await session.commit()

    return FutureResponse(
        type="create_runtime",
        future_id=str(future.request_id),
        request_id=str(future.request_id),
        runtime_id=runtime.runtime_id,
        status=future.status,
    )


@router.get("/api/v1/runtimes/{runtime_id}", response_model=RuntimeStatusResponse)
async def get_runtime(
    runtime_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> RuntimeStatusResponse:
    runtime = await session.get(RuntimeInstanceRecord, runtime_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Runtime not found")
    return _runtime_status(runtime)


@router.post("/api/v1/runtimes/{runtime_id}/heartbeat", response_model=RuntimeHeartbeatResponse)
async def runtime_heartbeat(
    runtime_id: str,
    body: RuntimeHeartbeatRequest,
    session: AsyncSession = Depends(get_db_session),
) -> RuntimeHeartbeatResponse:
    try:
        runtime = await record_runtime_heartbeat(session, runtime_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return RuntimeHeartbeatResponse(runtime_id=runtime.runtime_id, status=runtime.status, ready=runtime.ready)


@router.post("/api/v1/runtime_heartbeat", response_model=RuntimeHeartbeatResponse)
async def runtime_heartbeat_compat(
    body: RuntimeHeartbeatRequest,
    session: AsyncSession = Depends(get_db_session),
) -> RuntimeHeartbeatResponse:
    if not body.runtime_id:
        raise HTTPException(status_code=400, detail="runtime_id is required")
    return await runtime_heartbeat(body.runtime_id, body, session)
