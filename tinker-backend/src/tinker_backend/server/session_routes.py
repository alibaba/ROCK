"""Session management endpoints."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    SessionHeartbeatRequest,
    SessionHeartbeatResponse,
)
from tinker_backend.server.deps import get_db_session
from tinker_backend.storage.models import ClientSessionRecord

router = APIRouter()


@router.post("/api/v1/create_session", response_model=CreateSessionResponse)
async def create_session(
    request: CreateSessionRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CreateSessionResponse:
    session_id = "session_" + uuid4().hex[:12]
    session.add(
        ClientSessionRecord(
            session_id=session_id,
            tags=request.tags,
            user_metadata=request.user_metadata or {},
            sdk_version=request.sdk_version,
            project_id=request.project_id,
            status="active",
        )
    )
    await session.commit()
    return CreateSessionResponse(session_id=session_id)


@router.post("/api/v1/session_heartbeat", response_model=SessionHeartbeatResponse)
async def session_heartbeat(
    request: SessionHeartbeatRequest,
    session: AsyncSession = Depends(get_db_session),
) -> SessionHeartbeatResponse:
    session_db = await session.get(ClientSessionRecord, request.session_id)
    if session_db is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session_db.last_heartbeat_at = datetime.now(timezone.utc)
    session_db.heartbeat_count += 1
    await session.commit()
    return SessionHeartbeatResponse()
