import asyncio
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile, WebSocket, WebSocketDisconnect

from rock.actions import (
    CloseResponse,
    EnvCloseRequest,
    EnvCloseResponse,
    EnvListResponse,
    EnvMakeRequest,
    EnvMakeResponse,
    EnvResetRequest,
    EnvResetResponse,
    EnvStepRequest,
    EnvStepResponse,
    UploadResponse,
)
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseSessionRequest as CloseSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.common.port_validation import validate_port_forward_port
from rock.logger import init_logger
from rock.rocklet.local_sandbox import LocalSandboxRuntime
from rock.utils import get_executor

logger = init_logger(__name__)

local_router = APIRouter()

# Timeouts for port forwarding
TCP_CONNECT_TIMEOUT = 10  # seconds
IDLE_TIMEOUT = 300  # seconds

runtime = LocalSandboxRuntime(executor=get_executor())


def serialize_model(model):
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


@local_router.get("/is_alive")
async def is_alive():
    return serialize_model(await runtime.is_alive())


@local_router.get("/get_statistics")
async def get_statistics():
    return await runtime.get_statistics()


@local_router.post("/create_session")
async def create_session(request: CreateSessionRequest):
    return serialize_model(await runtime.create_session(request))


@local_router.post("/run_in_session")
async def run(action: Action):
    return serialize_model(await runtime.run_in_session(action))


@local_router.post("/close_session")
async def close_session(request: CloseSessionRequest):
    return serialize_model(await runtime.close_session(request))


@local_router.post("/execute")
async def execute(command: Command):
    return serialize_model(await runtime.execute(command=command))


@local_router.post("/read_file")
async def read_file(request: ReadFileRequest):
    return serialize_model(await runtime.read_file(request))


@local_router.post("/write_file")
async def write_file(request: WriteFileRequest):
    return serialize_model(await runtime.write_file(request))


@local_router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    target_path: str = Form(...),  # type: ignore
    unzip: bool = Form(False),
):
    target_path: Path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # First save the file to a temporary directory and potentially unzip it.
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = Path(temp_dir) / "temp_file_transfer"
        try:
            with open(file_path, "wb") as f:
                f.write(await file.read())
        finally:
            await file.close()
        if unzip:
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                zip_ref.extractall(target_path)
            file_path.unlink()
        else:
            shutil.move(file_path, target_path)
    return UploadResponse()


@local_router.post("/close")
async def close():
    await runtime.close()
    return CloseResponse()


@local_router.post("/env/make")
async def env_make(request: EnvMakeRequest) -> EnvMakeResponse:
    return runtime.env_make(env_id=request.env_id, sandbox_id=request.sandbox_id)


@local_router.post("/env/step")
async def env_step(request: EnvStepRequest) -> EnvStepResponse:
    return runtime.env_step(request.sandbox_id, request.action)


@local_router.post("/env/reset")
async def env_reset(request: EnvResetRequest) -> EnvResetResponse:
    return runtime.env_reset(request.sandbox_id, request.seed)


@local_router.post("/env/close")
async def env_close(request: EnvCloseRequest) -> EnvCloseResponse:
    return runtime.env_close(request.sandbox_id)


@local_router.post("/env/list")
async def env_list() -> EnvListResponse:
    return runtime.env_list()


@local_router.websocket("/portforward")
async def portforward(websocket: WebSocket, port: int):
    """WebSocket endpoint for TCP port forwarding.

    This endpoint proxies WebSocket connections to a local TCP port,
    allowing external clients to access TCP services running inside
    the sandbox container.

    Args:
        websocket: WebSocket connection from client
        port: Target TCP port to connect to (query parameter)
    """
    client_host = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else "unknown"
    logger.info(
        f"[Portforward] Request received: target_port={port}, "
        f"client={client_host}:{client_port}, path={websocket.url.path}"
    )

    logger.info(f"[Portforward] Accepting WebSocket: target_port={port}")
    await websocket.accept()
    logger.info(f"[Portforward] WebSocket accepted: target_port={port}")

    # Validate port
    logger.debug(f"[Portforward] Validating port: target_port={port}")
    is_valid, error_message = validate_port_forward_port(port)
    if not is_valid:
        logger.warning(f"[Portforward] Port validation failed: target_port={port}, reason={error_message}")
        await websocket.close(code=1008, reason=error_message)
        return
    logger.info(f"[Portforward] Port validation passed: target_port={port}")

    logger.info(
        f"[Portforward] Connecting to local TCP: target_port={port}, "
        f"address=127.0.0.1:{port}, timeout={TCP_CONNECT_TIMEOUT}s"
    )

    try:
        # Connect to local TCP port
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=TCP_CONNECT_TIMEOUT)
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=TCP_CONNECT_TIMEOUT)
        logger.info(
            f"[Portforward] TCP connection established: target_port={port}, "
            f"local_addr={writer.get_extra_info('sockname')}"
        )
    except asyncio.TimeoutError:
        logger.error(f"[Portforward] TCP connection timeout: target_port={port}, timeout={TCP_CONNECT_TIMEOUT}s")
        await websocket.close(code=1011, reason=f"Connection to port {port} timed out")
        return
    except OSError as e:
        logger.error(
            f"[Portforward] TCP connection failed: target_port={port}, "
            f"error_type={type(e).__name__}, errno={e.errno}, error={e}"
        )
        await websocket.close(code=1011, reason=f"Failed to connect to port {port}: {e}")
        return
    except Exception as e:
        logger.error(
            f"[Portforward] Unexpected TCP error: target_port={port}, error_type={type(e).__name__}, error={e}"
        )
        await websocket.close(code=1011, reason=f"Unexpected error: {e}")
        return

    logger.info(f"[Portforward] Starting bidirectional forwarding: target_port={port}")

    ws_to_tcp_bytes = 0
    ws_to_tcp_msgs = 0
    tcp_to_ws_bytes = 0
    tcp_to_ws_msgs = 0

    async def ws_to_tcp():
        """Forward messages from WebSocket to TCP."""
        nonlocal ws_to_tcp_bytes, ws_to_tcp_msgs
        try:
            while True:
                data = await websocket.receive_bytes()
                ws_to_tcp_msgs += 1
                ws_to_tcp_bytes += len(data)
                writer.write(data)
                await writer.drain()
                logger.debug(
                    f"[Portforward] ws->tcp: target_port={port}, "
                    f"bytes={len(data)}, total_msgs={ws_to_tcp_msgs}, total_bytes={ws_to_tcp_bytes}"
                )
        except WebSocketDisconnect as e:
            logger.info(f"[Portforward] ws->tcp: client disconnected: target_port={port}, code={e.code}")
        except Exception as e:
            logger.debug(f"[Portforward] ws->tcp error: target_port={port}, error_type={type(e).__name__}, error={e}")
        finally:
            writer.close()

    async def tcp_to_ws():
        """Forward data from TCP to WebSocket."""
        nonlocal tcp_to_ws_bytes, tcp_to_ws_msgs
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    logger.info(f"[Portforward] tcp->ws: TCP connection closed by peer: target_port={port}")
                    break
                tcp_to_ws_msgs += 1
                tcp_to_ws_bytes += len(data)
                await websocket.send_bytes(data)
                logger.debug(
                    f"[Portforward] tcp->ws: target_port={port}, "
                    f"bytes={len(data)}, total_msgs={tcp_to_ws_msgs}, total_bytes={tcp_to_ws_bytes}"
                )
        except Exception as e:
            logger.debug(f"[Portforward] tcp->ws error: target_port={port}, error_type={type(e).__name__}, error={e}")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    # Run both directions concurrently
    try:
        await asyncio.gather(ws_to_tcp(), tcp_to_ws())
    except Exception as e:
        logger.debug(f"[Portforward] Forwarding error: target_port={port}, error_type={type(e).__name__}, error={e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        logger.info(
            f"[Portforward] Connection closed: target_port={port}, "
            f"ws->tcp: {ws_to_tcp_msgs} msgs, {ws_to_tcp_bytes} bytes, "
            f"tcp->ws: {tcp_to_ws_msgs} msgs, {tcp_to_ws_bytes} bytes"
        )
