"""Health, readiness, config, and root endpoints."""

from typing import Any

from fastapi import APIRouter

from tinker_backend.config import get_settings
from tinker_backend.protocol.schemas import (
    ClientConfigRequest,
    ClientConfigResponse,
    HealthResponse,
    ServerCapabilitiesResponse,
)

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
@router.get("/api/v1/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz")
async def readyz() -> dict[str, Any]:
    settings = get_settings()
    checks = settings.readiness_checks()
    return {"ready": all(checks.values()), "checks": checks}


@router.get("/configz")
async def configz() -> dict[str, Any]:
    return get_settings().redacted_view()


@router.post("/api/v1/client/config", response_model=ClientConfigResponse)
async def client_config(_request: ClientConfigRequest | None = None) -> ClientConfigResponse:
    return ClientConfigResponse()


@router.post("/api/v1/telemetry")
async def telemetry() -> dict[str, str]:
    return {"status": "accepted"}


@router.get("/api/v1/get_server_capabilities", response_model=ServerCapabilitiesResponse)
async def get_server_capabilities() -> ServerCapabilitiesResponse:
    return ServerCapabilitiesResponse()


@router.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "tinker-backend",
        "mode": "pg-only-runtime-mvp",
        "endpoints": [
            "/api/v1/create_session",
            "/api/v1/session_heartbeat",
            "/api/v1/runtimes",
            "/api/v1/retrieve_future",
            "/api/v1/sdk/{runtime_id}/init_task_env",
            "/api/v1/sdk/{runtime_id}/get_step",
            "/api/v1/sdk/{runtime_id}/asample",
            "/api/v1/sdk/{runtime_id}/close",
            "/api/v1/runtimes/{runtime_id}/actions/claim",
            "/api/v1/runtimes/{runtime_id}/heartbeat",
        ],
    }
