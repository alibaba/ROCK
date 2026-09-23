import math
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from rock.actions.sandbox.response import State
from rock.admin.proto.e2b_snapshot import (
    E2BCreateSnapshotRequest,
    E2BCreateTemplateRequest,
    E2BCreateTemplateResponse,
    E2BForkRequest,
    E2BForkResult,
    E2BSnapshot,
    E2BStartTemplateBuildRequest,
    E2BTemplate,
)
from rock.admin.proto.request import E2BCreateSandboxRequest, E2BSetSandboxTimeoutRequest, StartHeaders
from rock.admin.proto.response import E2BCreateSandboxResponse, E2BSandboxInfo
from rock.admin.service.e2b_service import E2BSandboxUnavailableError, E2BService
from rock.admin.service.e2b_snapshot_service import E2BSnapshotTimeout
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotNotFound
from rock.common.constants import AP_SANDBOX_ID_METADATA_KEY, E2B_CLIENT_ID, E2B_ENVD_VERSION
from rock.common.validation import NonBlankStr
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.snapshot_types import ManagerResultUnknown
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError

logger = init_logger(__name__)


class E2BAPIRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await route_handler(request)
            except RequestValidationError as error:
                message = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
                )
                return _error_response(400, message)
            except E2BSandboxNotFoundError as error:
                return _error_response(404, str(error))
            except SnapshotNotFound as error:
                return _error_response(404, str(error))
            except SnapshotConflict as error:
                return _error_response(409, str(error))
            except ManagerResultUnknown as error:
                return _error_response(503, str(error))
            except E2BSandboxUnavailableError as error:
                return _error_response(409, str(error))
            except E2BSnapshotTimeout as error:
                return _error_response(504, str(error))
            except TimeoutError as error:
                return _error_response(504, str(error))
            except BadRequestRockError as error:
                logger.warning("E2B request rejected: %s", error)
                return _error_response(400, str(error))
            except ValueError as error:
                return _error_response(400, str(error))
            except Exception:
                logger.exception("E2B request failed")
                return _error_response(500, "Internal server error")

        return handler


e2b_router = APIRouter(route_class=E2BAPIRoute)
e2b_service: E2BService


def set_e2b_service(service: E2BService) -> None:
    global e2b_service
    e2b_service = service


set_e2b_sandbox_manager = set_e2b_service


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": status_code, "message": message})


@e2b_router.post(
    "/sandboxes",
    status_code=201,
    response_model=E2BCreateSandboxResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def create_sandbox(
    request: E2BCreateSandboxRequest,
    response: Response,
    headers: Annotated[StartHeaders, Depends()],
) -> E2BCreateSandboxResponse:
    # ROCK stores lifecycle TTLs in whole minutes. Round up so an E2B timeout
    # never expires a sandbox earlier than the caller requested.
    config = DockerDeploymentConfig(
        image=request.template_id,
        template_id=request.template_id,
        auto_clear_time_minutes=math.ceil(request.timeout / 60),
        container_name=request.metadata.get(AP_SANDBOX_ID_METADATA_KEY),
        metadata=request.metadata,
        env_vars=request.env_vars,
    )
    user_info = dict(headers.user_info)
    result = await e2b_service.start(
        config,
        user_info=user_info,
        cluster_info=headers.cluster_info,
    )
    response.headers["Cache-Control"] = "no-store"
    return E2BCreateSandboxResponse(
        sandboxID=result.sandbox_id,
        envdVersion=E2B_ENVD_VERSION,
        clientID=E2B_CLIENT_ID,
        templateID=request.template_id,
        envdAccessToken=(await e2b_service.get_sandbox_access_token(result.sandbox_id))
        or headers.api_key,
    )


@e2b_router.get(
    "/sandboxes/{sandboxID}",
    response_model=E2BSandboxInfo,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def get_sandbox(
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
) -> E2BSandboxInfo:
    return await e2b_service.get_sandbox(sandbox_id)


@e2b_router.post("/sandboxes/{sandboxID}/timeout", status_code=204, response_class=Response)
async def set_sandbox_timeout(
    request: E2BSetSandboxTimeoutRequest,
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
) -> Response:
    await e2b_service.set_timeout(sandbox_id, request.timeout)
    return Response(status_code=204)


@e2b_router.delete("/sandboxes/{sandboxID}", status_code=204, response_class=Response)
async def delete_sandbox(
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
) -> Response:
    try:
        status = await e2b_service.get_status(sandbox_id, include_all_states=True)
    except BadRequestRockError as error:
        return _error_response(404, str(error))
    if status.state == State.DELETED:
        return _error_response(404, f"Sandbox {sandbox_id} not found")
    needs_stop = status.state == State.PENDING or (
        status.state == State.RUNNING and not e2b_service.supports_running_delete
    )
    if needs_stop:
        await e2b_service.stop(sandbox_id)
    await e2b_service.delete(sandbox_id)
    return Response(status_code=204)


class E2BPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory: Literal[True] = True


class E2BConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout: int = Field(default=300, gt=0, strict=True)


@e2b_router.post("/sandboxes/{sandboxID}/pause", status_code=204, response_class=Response)
async def pause_sandbox(
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
    request: E2BPauseRequest | None = None,
) -> Response:
    if not await e2b_service.pause(sandbox_id):
        return _error_response(409, f"Sandbox {sandbox_id} is already paused")
    return Response(status_code=204)


@e2b_router.post(
    "/sandboxes/{sandboxID}/connect",
    response_model=E2BCreateSandboxResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
@e2b_router.post(
    "/v2/sandboxes/{sandboxID}/connect",
    response_model=E2BCreateSandboxResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def connect_sandbox(
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
    response: Response,
    request: E2BConnectRequest | None = None,
) -> E2BCreateSandboxResponse:
    resumed = await e2b_service.connect(sandbox_id, timeout=(request or E2BConnectRequest()).timeout)
    info = await e2b_service.get_sandbox(sandbox_id)
    response.status_code = 201 if resumed else 200
    return E2BCreateSandboxResponse(
        sandboxID=sandbox_id,
        templateID=info.template_id,
        envdVersion=E2B_ENVD_VERSION,
        clientID=E2B_CLIENT_ID,
        envdAccessToken=info.envd_access_token,
    )


def _snapshot_service():
    service = e2b_service.snapshot_service
    if service is None:
        raise RuntimeError("snapshot service is not ready")
    return service


@e2b_router.post(
    "/sandboxes/{sandboxID}/snapshots",
    status_code=201,
    response_model=E2BSnapshot,
    response_model_by_alias=True,
)
async def create_snapshot(
    request: E2BCreateSnapshotRequest,
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
) -> E2BSnapshot:
    return await _snapshot_service().create_snapshot(sandbox_id, request)


@e2b_router.post(
    "/sandboxes/{sandboxID}/fork",
    status_code=201,
    response_model=list[E2BForkResult],
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def fork_sandbox(
    request: E2BForkRequest,
    sandbox_id: Annotated[NonBlankStr, Path(alias="sandboxID")],
) -> list[E2BForkResult]:
    return await _snapshot_service().fork_sandbox(sandbox_id, request)


@e2b_router.get(
    "/snapshots",
    response_model=list[E2BSnapshot],
    response_model_by_alias=True,
)
async def list_snapshots(
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    next_token: Annotated[str | None, Query(alias="nextToken")] = None,
) -> list[E2BSnapshot]:
    snapshots, cursor = await _snapshot_service().list_snapshots(
        limit=limit,
        next_token=next_token,
    )
    if cursor is not None:
        response.headers["X-Next-Token"] = cursor
    return snapshots


@e2b_router.get(
    "/templates/{templateID}",
    response_model=E2BTemplate,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def get_template(
    template_id: Annotated[NonBlankStr, Path(alias="templateID")],
) -> E2BTemplate:
    return await _snapshot_service().get_template(template_id)


@e2b_router.delete("/templates/{templateID}", status_code=204, response_class=Response)
async def delete_template(
    template_id: Annotated[NonBlankStr, Path(alias="templateID")],
) -> Response:
    await _snapshot_service().delete_template(template_id)
    return Response(status_code=204)


@e2b_router.post(
    "/v3/templates",
    status_code=202,
    response_model=E2BCreateTemplateResponse,
    response_model_by_alias=True,
)
async def create_template(
    request: E2BCreateTemplateRequest,
) -> E2BCreateTemplateResponse:
    return await _snapshot_service().create_template(request)


@e2b_router.post("/v2/templates/{templateID}/builds/{buildID}", status_code=202, response_class=Response)
async def start_template_build(
    request: E2BStartTemplateBuildRequest,
    template_id: Annotated[NonBlankStr, Path(alias="templateID")],
    build_id: Annotated[NonBlankStr, Path(alias="buildID")],
) -> Response:
    await _snapshot_service().start_template_build(template_id, build_id, request)
    return Response(status_code=202)
