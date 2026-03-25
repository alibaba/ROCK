import asyncio
from typing import Any

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse as _JSONResponse

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    ResponseStatus,
    RockResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.admin.proto.request import (
    BatchSandboxStatusRequest,
    SandboxBashAction,
    SandboxCloseBashSessionRequest,
    SandboxCommand,
    SandboxCreateBashSessionRequest,
    SandboxQueryParams,
    SandboxReadFileRequest,
    SandboxWriteFileRequest,
)
from rock.admin.proto.response import BatchSandboxStatusResponse, SandboxListResponse
from rock.common.exception import handle_exceptions
from rock.common.port_validation import validate_port_forward_port
from rock.logger import init_logger
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)

sandbox_proxy_router = APIRouter()
sandbox_proxy_service: SandboxProxyService


def set_sandbox_proxy_service(service: SandboxProxyService):
    global sandbox_proxy_service
    sandbox_proxy_service = service


@sandbox_proxy_router.post("/execute")
@handle_exceptions(error_message="execute command failed")
async def execute(command: SandboxCommand) -> RockResponse[CommandResponse]:
    return RockResponse(result=await sandbox_proxy_service.execute(command))


@sandbox_proxy_router.post("/create_session")
@handle_exceptions(error_message="create session failed")
async def create_session(request: SandboxCreateBashSessionRequest) -> RockResponse[CreateBashSessionResponse]:
    return RockResponse(result=await sandbox_proxy_service.create_session(request))


@sandbox_proxy_router.post("/run_in_session")
@handle_exceptions(error_message="run in session failed")
async def run(action: SandboxBashAction) -> RockResponse[BashObservation]:
    result = await sandbox_proxy_service.run_in_session(action)
    if result.exit_code is not None and result.exit_code == -1:
        return RockResponse(status=ResponseStatus.FAILED, error=result.failure_reason)
    return RockResponse(result=result)


@sandbox_proxy_router.post("/sandboxes/batch")
@handle_exceptions(error_message="batch get sandbox status failed")
async def batch_get_status(request: BatchSandboxStatusRequest) -> RockResponse[BatchSandboxStatusResponse]:
    statuses_list = await sandbox_proxy_service.batch_get_sandbox_status_from_redis(request.sandbox_ids)
    response = BatchSandboxStatusResponse(statuses=statuses_list)
    return RockResponse(result=response)


@sandbox_proxy_router.post("/close_session")
@handle_exceptions(error_message="close session failed")
async def close_session(request: SandboxCloseBashSessionRequest) -> RockResponse[CloseBashSessionResponse]:
    return RockResponse(result=await sandbox_proxy_service.close_session(request))


@sandbox_proxy_router.get("/is_alive")
@handle_exceptions(error_message="get sandbox is alive failed")
async def is_alive(sandbox_id: str):
    return RockResponse(result=await sandbox_proxy_service.is_alive(sandbox_id))


@sandbox_proxy_router.post("/read_file")
@handle_exceptions(error_message="read file failed")
async def read_file(request: SandboxReadFileRequest) -> RockResponse[ReadFileResponse]:
    return RockResponse(result=await sandbox_proxy_service.read_file(request))


@sandbox_proxy_router.post("/write_file")
@handle_exceptions(error_message="write file failed")
async def write_file(request: SandboxWriteFileRequest) -> RockResponse[WriteFileResponse]:
    return RockResponse(result=await sandbox_proxy_service.write_file(request))


@sandbox_proxy_router.post("/upload")
@handle_exceptions(error_message="upload file failed")
async def upload(
    file: UploadFile = File(...),
    target_path: str = Form(...),
    sandbox_id: str | None = Form(None),
) -> RockResponse[UploadResponse]:
    return RockResponse(result=await sandbox_proxy_service.upload(file, target_path, sandbox_id))


@sandbox_proxy_router.get("/sandboxes")
@handle_exceptions(error_message="list sandboxes failed")
async def list_sandboxes(request: Request) -> RockResponse[SandboxListResponse]:
    query_params: SandboxQueryParams = dict(request.query_params)
    result = await sandbox_proxy_service.list_sandboxes(query_params)
    return RockResponse(result=result)


@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/port/{rock_target_port}/ws")
@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/port/{rock_target_port}/ws/{path:path}")
async def websocket_proxy_with_port(
    websocket: WebSocket, id: str, rock_target_port: int, path: str = ""
):
    sandbox_id = id
    logger.info(
        f"Client connected to WebSocket proxy (path-based port): {sandbox_id}, path: {path}, port: {rock_target_port}"
    )
    is_valid, error_msg = validate_port_forward_port(rock_target_port)
    if not is_valid:
        await websocket.close(code=1008, reason=error_msg)
        return

    await websocket.accept()
    try:
        await sandbox_proxy_service.websocket_proxy(websocket, sandbox_id, path, port=rock_target_port)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from WebSocket proxy: {sandbox_id}")
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
        await websocket.close(code=1011, reason=f"Proxy error: {str(e)}")


@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws")
@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws/{path:path}")
async def websocket_proxy(websocket: WebSocket, id: str, path: str = "", rock_target_port: int | None = Query(None)):
    sandbox_id = id
    logger.info(f"Client connected to WebSocket proxy: {sandbox_id}, path: {path}, port: {rock_target_port}")

    if rock_target_port is not None:
        is_valid, error_msg = validate_port_forward_port(rock_target_port)
        if not is_valid:
            await websocket.close(code=1008, reason=error_msg)
            return

    await websocket.accept()
    try:
        await sandbox_proxy_service.websocket_proxy(websocket, sandbox_id, path, port=rock_target_port)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from WebSocket proxy: {sandbox_id}")
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
        await websocket.close(code=1011, reason=f"Proxy error: {str(e)}")


@sandbox_proxy_router.websocket("/sandboxes/{id}/portforward")
async def portforward(websocket: WebSocket, id: str, port: int):
    """
    WebSocket TCP port forwarding endpoint.

    Proxies a WebSocket connection to a TCP port inside the sandbox.

    Args:
        id: The sandbox identifier.
        port: The target TCP port inside the sandbox.

    Errors:
        - Port validation failure: Connection closed with error
        - Sandbox not found: Connection closed with error
        - TCP connection failure: Connection closed with error
    """
    sandbox_id = id
    client_host = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else "unknown"
    logger.info(
        f"[Portforward] Request received: sandbox={sandbox_id}, target_port={port}, "
        f"client={client_host}:{client_port}, path={websocket.url.path}"
    )

    try:
        logger.info(f"[Portforward] Accepting WebSocket connection: sandbox={sandbox_id}, target_port={port}")
        await websocket.accept()
        logger.info(
            f"[Portforward] WebSocket accepted, calling proxy service: sandbox={sandbox_id}, target_port={port}"
        )
        await sandbox_proxy_service.websocket_to_tcp_proxy(websocket, sandbox_id, port)
        logger.info(f"[Portforward] Proxy service completed: sandbox={sandbox_id}, target_port={port}")
    except ValueError as e:
        logger.warning(f"[Portforward] Validation failed: sandbox={sandbox_id}, target_port={port}, error={e}")
        await websocket.close(code=1008, reason=str(e))
    except WebSocketDisconnect as e:
        logger.info(f"[Portforward] Client disconnected: sandbox={sandbox_id}, target_port={port}, code={e.code}")
    except Exception as e:
        logger.error(
            f"[Portforward] Unexpected error: sandbox={sandbox_id}, target_port={port}, "
            f"error_type={type(e).__name__}, error={e}",
            exc_info=True,
        )
        await websocket.close(code=1011, reason=f"Proxy error: {str(e)}")


@sandbox_proxy_router.get("/get_token")
@handle_exceptions(error_message="get oss sts token failed")
async def get_token():
    result = await asyncio.to_thread(sandbox_proxy_service.gen_oss_sts_token)
    return RockResponse(result=result)


@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy/port/{rock_target_port}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy/port/{rock_target_port}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@handle_exceptions(error_message="http proxy failed")
async def http_proxy_with_port(
    sandbox_id: str,
    request: Request,
    rock_target_port: int,
    path: str = "",
):
    is_valid, error_msg = validate_port_forward_port(rock_target_port)
    if not is_valid:
        return _JSONResponse(status_code=400, content={"detail": error_msg})

    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        try:
            body = await request.json()
        except Exception:
            body = None
    # Build proxy_prefix (path only, no scheme/host) for Location header rewrite
    proxy_prefix = request.url.path.rstrip(path).rstrip("/")
    return await sandbox_proxy_service.http_proxy(
        sandbox_id, path, body, request.headers, method=request.method, port=rock_target_port,
        proxy_prefix=proxy_prefix,
        query_string=str(request.url.query),
    )


@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@handle_exceptions(error_message="http proxy failed")
async def http_proxy(
    sandbox_id: str,
    request: Request,
    path: str = "",
    rock_target_port: int | None = Query(None),
):
    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        try:
            body = await request.json()
        except Exception:
            body = None
    return await sandbox_proxy_service.http_proxy(
        sandbox_id, path, body, request.headers, method=request.method, port=rock_target_port
    )


@sandbox_proxy_router.post("/host/proxy/{path:path}")
@handle_exceptions(error_message="host proxy post failed")
async def host_proxy_post(
    request: Request,
    path: str,
    body: dict[str, Any] = Body(...),
):
    host_ip = request.headers.get("rock-host-ip")
    if not host_ip:
        raise BadRequestRockError("rock-host-ip is required in request headers")
    return await sandbox_proxy_service.host_proxy(host_ip, path, body, request.headers)
