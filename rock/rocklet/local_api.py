import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

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
from rock.rocklet.local_sandbox import LocalSandboxRuntime
from rock.utils import get_executor

local_router = APIRouter()

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
