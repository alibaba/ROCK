from typing import Any

from fastapi import APIRouter, Body, HTTPException

from rock.actions import (
    EnvCloseRequest,
    EnvCloseResponse,
    EnvListResponse,
    EnvMakeResponse,
    EnvResetRequest,
    EnvResetResponse,
    EnvStepRequest,
    EnvStepResponse,
)
from rock.sandbox.gem_manager import GemManager


def _require_sandbox_id(sandbox_id: str | None) -> None:
    if sandbox_id is None or not sandbox_id.strip():
        raise HTTPException(status_code=400, detail="sandbox_id is required and must be a non-empty string")


gem_router = APIRouter()
sandbox_manager: GemManager


def set_env_service(service: GemManager):
    global sandbox_manager
    sandbox_manager = service


@gem_router.post("/make")
async def env_make(request: dict[str, Any] = Body(...)) -> EnvMakeResponse:
    env_id = request.get("env_id")
    if not env_id:
        raise ValueError("env_id is required")
    return await sandbox_manager.env_make(env_id)


@gem_router.post("/step")
async def env_step(request: EnvStepRequest) -> EnvStepResponse:
    _require_sandbox_id(request.sandbox_id)
    return await sandbox_manager.env_step(request)


@gem_router.post("/reset")
async def env_reset(request: EnvResetRequest) -> EnvResetResponse:
    _require_sandbox_id(request.sandbox_id)
    return await sandbox_manager.env_reset(request)


@gem_router.post("/close")
async def env_close(request: EnvCloseRequest) -> EnvCloseResponse:
    _require_sandbox_id(request.sandbox_id)
    return await sandbox_manager.env_close(request)


@gem_router.post("/list")
async def env_list(request: dict[str, Any] = Body(...)) -> EnvListResponse:
    # Get available environments from already started sandboxes
    sandbox_id = request.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required")

    return await sandbox_manager.env_list(sandbox_id=sandbox_id)
