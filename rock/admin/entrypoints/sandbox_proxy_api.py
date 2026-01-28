import asyncio
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect

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
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.utils import handle_exceptions

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
    if result.exit_code is not None and result.exit_code != 0:
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


@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws")
@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws/{path:path}")
async def websocket_proxy(websocket: WebSocket, id: str, path: str = ""):
    await websocket.accept()
    sandbox_id = id
    logging.info(f"Client connected to WebSocket proxy: {sandbox_id}, path: {path}")
    try:
        await sandbox_proxy_service.websocket_proxy(websocket, sandbox_id, path)
    except WebSocketDisconnect:
        logging.info(f"Client disconnected from WebSocket proxy: {sandbox_id}")
    except Exception as e:
        logging.error(f"WebSocket proxy error: {e}")
        await websocket.close(code=1011, reason=f"Proxy error: {str(e)}")


@sandbox_proxy_router.get("/get_token")
@handle_exceptions(error_message="get oss sts token failed")
async def get_token():
    result = await asyncio.to_thread(sandbox_proxy_service.gen_oss_sts_token)
    return RockResponse(result=result)
