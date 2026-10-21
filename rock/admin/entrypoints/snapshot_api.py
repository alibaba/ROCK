"""Canonical public routes rooted at the snapshot resource."""

from typing import Annotated

from fastapi import APIRouter, Query

from rock.actions import RockResponse
from rock.admin.proto.snapshot import (
    CreateBaseImageRequest,
    SnapshotResponse,
    StartFromSnapshotRequest,
    StartFromSnapshotResponse,
)
from rock.admin.service.snapshot_manager import SnapshotManager
from rock.common.exception import handle_exceptions
from rock.sdk.common.exceptions import InternalServerRockError

snapshot_router = APIRouter()
snapshot_manager: SnapshotManager | None = None


def set_snapshot_manager(orchestrator: SnapshotManager | None) -> None:
    global snapshot_manager
    snapshot_manager = orchestrator


def _service() -> SnapshotManager:
    if snapshot_manager is None:
        raise InternalServerRockError("snapshot service is not ready")
    return snapshot_manager


@snapshot_router.post("/snapshots/base-images")
@handle_exceptions(error_message="create base image failed")
async def create_base_image(request: CreateBaseImageRequest) -> RockResponse:
    record = await _service().create_base_image(request)
    snapshot = SnapshotResponse.from_record(record)
    return RockResponse(result=snapshot)


@snapshot_router.post("/snapshots/{snapshot_id}/start_async")
@handle_exceptions(error_message="start sandbox from snapshot failed")
async def start_from_snapshot(
    snapshot_id: str,
    request: StartFromSnapshotRequest,
):
    result = await _service().start_async(snapshot_id, request)
    return RockResponse(result=StartFromSnapshotResponse.model_validate(result))


@snapshot_router.get("/snapshots/{snapshot_id}")
@handle_exceptions(error_message="get snapshot failed")
async def get_snapshot(snapshot_id: str):
    record = await _service().get_snapshot_record(snapshot_id)
    return RockResponse(result=SnapshotResponse.from_record(record))


@snapshot_router.get("/snapshots")
@handle_exceptions(error_message="list snapshots failed")
async def list_snapshots(
    source_sandbox_id: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=1000)] = 100,
):
    records = await _service().list_snapshot_records(source_sandbox_id=source_sandbox_id, offset=offset, limit=limit)
    return RockResponse(result=[SnapshotResponse.from_record(record) for record in records])


@snapshot_router.delete("/snapshots/{snapshot_id}")
@handle_exceptions(error_message="delete snapshot failed")
async def delete_snapshot(snapshot_id: str):
    record = await _service().delete_snapshot(snapshot_id)
    return RockResponse(result=SnapshotResponse.from_record(record))
