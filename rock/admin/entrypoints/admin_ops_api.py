"""Admin ops API: thin routing layer for TaskSet operations.

Endpoints (relative to prefix /apis/envs/sandbox/v1/ops):
- POST /tasksets             Create a TaskSet (triggers tasks on workers)
- GET  /tasksets/{taskset_id}  Query TaskSet with child tasks
"""

from fastapi import APIRouter, Body, Request

from rock.actions.response import ResponseStatus, RockResponse
from rock.admin.proto.request import CreateTaskSetRequest
from rock.admin.service.ops_service import OpsService
from rock.common.exception import handle_exceptions

admin_ops_router = APIRouter()

_ops_service: OpsService | None = None


def set_ops_service(service: OpsService) -> None:
    global _ops_service
    _ops_service = service


@admin_ops_router.post("/tasksets")
@handle_exceptions(error_message="create taskset failed")
async def create_taskset(
    request: Request,
    payload: CreateTaskSetRequest = Body(default_factory=CreateTaskSetRequest),
) -> RockResponse[dict]:
    if _ops_service is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="ops service not initialised",
            error="server misconfigured",
        )

    caller = request.client.host if request.client else "unknown"
    resp = await _ops_service.create_taskset(payload.spec, caller)
    return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())


@admin_ops_router.get("/tasksets/{taskset_id}")
@handle_exceptions(error_message="get taskset failed")
async def get_taskset(taskset_id: str) -> RockResponse[dict]:
    if _ops_service is None:
        return RockResponse(
            status=ResponseStatus.FAILED,
            message="ops service not initialised",
            error="server misconfigured",
        )

    resp = await _ops_service.get_taskset(taskset_id)
    return RockResponse(status=ResponseStatus.SUCCESS, message="ok", result=resp.model_dump())
