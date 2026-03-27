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

VNC_PORT = 8006
X_ROCK_TARGET_PORT_HEADER = "X-ROCK-Target-Port"

sandbox_proxy_router = APIRouter()
sandbox_proxy_service: SandboxProxyService


def set_sandbox_proxy_service(service: SandboxProxyService):
    global sandbox_proxy_service
    sandbox_proxy_service = service


def resolve_target_port(request: Request, query_port: int | None, path: str) -> tuple[int | None, str]:
    """Resolve target port from path, header or query param.

    Priority:
    1. Path-based: /proxy/port/{port}/{path} -> extract port from path
    2. Header-based: X-ROCK-Target-Port header
    3. Query-based: rock_target_port query parameter

    Args:
        request: FastAPI Request object
        query_port: Port from query parameter
        path: Current path (may contain "port/{port}/...")

    Returns:
        Tuple of (resolved_port, remaining_path)

    Raises:
        BadRequestRockError: If both header and query param are specified
    """
    header_port_str = request.headers.get(X_ROCK_TARGET_PORT_HEADER)
    header_port = int(header_port_str) if header_port_str else None

    path_port = None
    remaining_path = path

    if path.startswith("port/"):
        parts = path.split("/", 2)
        if len(parts) >= 2:
            try:
                path_port = int(parts[1])
                remaining_path = parts[2] if len(parts) > 2 else ""
            except ValueError:
                pass

    port_sources = sum(
        [
            path_port is not None,
            header_port is not None,
            query_port is not None,
        ]
    )

    if port_sources > 1:
        sources = []
        if path_port is not None:
            sources.append("path")
        if header_port is not None:
            sources.append(f"header {X_ROCK_TARGET_PORT_HEADER}")
        if query_port is not None:
            sources.append("query parameter rock_target_port")
        raise BadRequestRockError(f"Cannot specify target port via multiple sources: {', '.join(sources)}")

    if path_port is not None:
        return path_port, remaining_path
    if header_port is not None:
        return header_port, path
    if query_port is not None:
        return query_port, path

    return None, path


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


def resolve_target_port_from_ws(websocket: WebSocket, query_port: int | None, path: str) -> tuple[int | None, str]:
    """Resolve target port for WebSocket connections.

    Same logic as resolve_target_port but for WebSocket (no Request object).
    """
    header_port_str = websocket.headers.get(X_ROCK_TARGET_PORT_HEADER)
    header_port = int(header_port_str) if header_port_str else None

    path_port = None
    remaining_path = path

    if path.startswith("port/"):
        parts = path.split("/", 2)
        if len(parts) >= 2:
            try:
                path_port = int(parts[1])
                remaining_path = parts[2] if len(parts) > 2 else ""
            except ValueError:
                pass

    port_sources = sum(
        [
            path_port is not None,
            header_port is not None,
            query_port is not None,
        ]
    )

    if port_sources > 1:
        sources = []
        if path_port is not None:
            sources.append("path")
        if header_port is not None:
            sources.append(f"header {X_ROCK_TARGET_PORT_HEADER}")
        if query_port is not None:
            sources.append("query parameter rock_target_port")
        raise BadRequestRockError(f"Cannot specify target port via multiple sources: {', '.join(sources)}")

    if path_port is not None:
        return path_port, remaining_path
    if header_port is not None:
        return header_port, path
    if query_port is not None:
        return query_port, path

    return None, path


@sandbox_proxy_router.websocket("/sandboxes/{sandbox_id}/vnc")
@sandbox_proxy_router.websocket("/sandboxes/{sandbox_id}/vnc/{path:path}")
async def vnc_websocket_proxy(
    websocket: WebSocket,
    sandbox_id: str,
    path: str = "",
):
    logger.info(f"Client connected to VNC WebSocket proxy: {sandbox_id}, path: {path}")
    try:
        await sandbox_proxy_service.websocket_proxy(websocket, sandbox_id, path, port=8006)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from VNC WebSocket proxy: {sandbox_id}")
    except Exception as e:
        logger.error(f"VNC WebSocket proxy error: {e}")
        await websocket.close(code=1011, reason=f"Proxy error: {str(e)}")


@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/{path:path}")
async def websocket_proxy(websocket: WebSocket, id: str, path: str = "", rock_target_port: int | None = Query(None)):
    sandbox_id = id

    try:
        port, resolved_path = resolve_target_port_from_ws(websocket, rock_target_port, path)
    except BadRequestRockError as e:
        await websocket.close(code=1008, reason=str(e))
        return

    logger.info(f"Client connected to WebSocket proxy: {sandbox_id}, path: {resolved_path}, port: {port}")

    if port is not None:
        is_valid, error_msg = validate_port_forward_port(port)
        if not is_valid:
            await websocket.close(code=1008, reason=error_msg)
            return

    try:
        await sandbox_proxy_service.websocket_proxy(websocket, sandbox_id, resolved_path, port=port)
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
    "/sandboxes/{sandbox_id}/vnc",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/vnc/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@handle_exceptions(error_message="vnc http proxy failed")
async def vnc_http_proxy(
    sandbox_id: str,
    request: Request,
    path: str = "",
):
    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        try:
            body = await request.json()
        except Exception:
            body = None
    proxy_prefix = request.url.path.rstrip(path).rstrip("/")
    return await sandbox_proxy_service.http_proxy(
        sandbox_id,
        path,
        body,
        request.headers,
        method=request.method,
        port=8006,
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
    try:
        port, resolved_path = resolve_target_port(request, rock_target_port, path)
    except BadRequestRockError as e:
        return _JSONResponse(status_code=400, content={"detail": str(e)})

    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        try:
            body = await request.json()
        except Exception:
            body = None

    proxy_prefix = request.url.path.rstrip(path).rstrip("/") if path else request.url.path.rstrip("/")
    return await sandbox_proxy_service.http_proxy(
        sandbox_id,
        resolved_path,
        body,
        request.headers,
        method=request.method,
        port=port,
        proxy_prefix=proxy_prefix,
        query_string=str(request.url.query),
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
