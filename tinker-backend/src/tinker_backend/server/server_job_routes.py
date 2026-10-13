"""ServerJob management endpoints used by the Tinker SDK local provider."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.server.deps import get_db_session
from tinker_backend.services.server_job_shutdown import stop_all_runtimes_for_server_job

router = APIRouter()


class ServerJobStopRequest(BaseModel):
    force_after_seconds: float = Field(default=30.0, ge=0.0)


@router.post("/api/v1/server_job/stop")
async def stop_server_job(
    body: ServerJobStopRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    request = body or ServerJobStopRequest()
    result = await stop_all_runtimes_for_server_job(
        session,
        force_after_seconds=float(request.force_after_seconds),
    )
    return {"status": "stopped", **result}
