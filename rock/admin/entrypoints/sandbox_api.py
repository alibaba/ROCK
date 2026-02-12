from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Form, UploadFile

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    IsAliveResponse,
    ReadFileResponse,
    RockResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.response import ResponseStatus
from rock.admin.proto.request import (
    SandboxBashAction,
    SandboxCloseBashSessionRequest,
    SandboxCommand,
    SandboxCreateBashSessionRequest,
    SandboxReadFileRequest,
    SandboxStartRequest,
    SandboxWriteFileRequest,
    StartHeaders,
)
from rock.admin.proto.response import SandboxStartResponse
from rock.common.constants import GET_STATUS_SWITCH, KATA_RUNTIME_SWITCH
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.utils import handle_exceptions

sandbox_router = APIRouter()
sandbox_manager: SandboxManager


def set_sandbox_manager(service: SandboxManager):
    global sandbox_manager
    sandbox_manager = service


async def _apply_kata_runtime_switch(config: DockerDeploymentConfig) -> None:
    """Check nacos switch and enable kata runtime on the config if the switch is on."""
    if (
        sandbox_manager.rock_config.nacos_provider is not None
        and await sandbox_manager.rock_config.nacos_provider.get_switch_status(KATA_RUNTIME_SWITCH)
    ):
        config.use_kata_runtime = True


@sandbox_router.post("/start")
@handle_exceptions(error_message="start sandbox failed")
async def start(request: SandboxStartRequest) -> RockResponse[SandboxStartResponse]:
    config = DockerDeploymentConfig.from_request(request)
    await _apply_kata_runtime_switch(config)
    sandbox_start_response = await sandbox_manager.start(config)
    return RockResponse(result=sandbox_start_response)


@sandbox_router.post("/start_async")
@handle_exceptions(error_message="async start sandbox failed")
async def start_async(
    request: SandboxStartRequest,
    headers: Annotated[StartHeaders, Depends()],
) -> RockResponse[SandboxStartResponse]:
    config = DockerDeploymentConfig.from_request(request)
    await _apply_kata_runtime_switch(config)
    sandbox_start_response = await sandbox_manager.start_async(
        config,
        user_info=headers.user_info,
        cluster_info=headers.cluster_info,
    )
    return RockResponse(result=sandbox_start_response)


@sandbox_router.get("/is_alive")
@handle_exceptions(error_message="get sandbox is alive failed")
async def is_alive(sandbox_id: str):
    try:
        status_response = await sandbox_manager.get_status(sandbox_id)
        alive_response = IsAliveResponse(is_alive=status_response.is_alive, message=status_response.host_name)
        return RockResponse(result=alive_response)
    except Exception:
        false_response = IsAliveResponse(is_alive=False, message=f"sandbox {sandbox_id} is alive failed")
        return RockResponse(result=false_response)


@sandbox_router.get("/get_sandbox_statistics")
@handle_exceptions(error_message="get sandbox statistics failed")
async def get_sandbox_statistics(sandbox_id: str):
    return RockResponse(result=await sandbox_manager.get_sandbox_statistics(sandbox_id))


@sandbox_router.get("/get_status")
@handle_exceptions(error_message="get sandbox status failed")
async def get_status(sandbox_id: str):
    # TODO: do judgement inside operator
    if (
        sandbox_manager.rock_config.nacos_provider is not None
        and await sandbox_manager.rock_config.nacos_provider.get_switch_status(GET_STATUS_SWITCH)
    ):
        return RockResponse(result=await sandbox_manager.get_status_v2(sandbox_id))
    return RockResponse(result=await sandbox_manager.get_status(sandbox_id))


@sandbox_router.post("/execute")
@handle_exceptions(error_message="execute command failed")
async def execute(command: SandboxCommand) -> RockResponse[CommandResponse]:
    return RockResponse(result=await sandbox_manager.execute(command))


@sandbox_router.post("/create_session")
@handle_exceptions(error_message="create session failed")
async def create_session(request: SandboxCreateBashSessionRequest) -> RockResponse[CreateBashSessionResponse]:
    return RockResponse(result=await sandbox_manager.create_session(request))


@sandbox_router.post("/run_in_session")
@handle_exceptions(error_message="run in session failed")
async def run(action: SandboxBashAction) -> RockResponse[BashObservation]:
    result = await sandbox_manager.run_in_session(action)
    if result.exit_code is not None and result.exit_code == -1:
        return RockResponse(status=ResponseStatus.FAILED, error=result.failure_reason)
    return RockResponse(result=result)


@sandbox_router.post("/close_session")
@handle_exceptions(error_message="close session failed")
async def close_session(request: SandboxCloseBashSessionRequest) -> RockResponse[CloseBashSessionResponse]:
    return RockResponse(result=await sandbox_manager.close_session(request))


@sandbox_router.post("/read_file")
@handle_exceptions(error_message="read file failed")
async def read_file(request: SandboxReadFileRequest) -> RockResponse[ReadFileResponse]:
    return RockResponse(result=await sandbox_manager.read_file(request))


@sandbox_router.post("/write_file")
@handle_exceptions(error_message="write file failed")
async def write_file(request: SandboxWriteFileRequest) -> RockResponse[WriteFileResponse]:
    return RockResponse(result=await sandbox_manager.write_file(request))


@sandbox_router.post("/upload")
@handle_exceptions(error_message="upload file failed")
async def upload(
    file: UploadFile = File(...),
    target_path: str = Form(...),
    sandbox_id: str | None = Form(None),
) -> RockResponse[UploadResponse]:
    return RockResponse(result=await sandbox_manager.upload(file, target_path, sandbox_id))


@sandbox_router.post("/stop")
@handle_exceptions(error_message="stop sandbox failed")
async def close(sandbox_id: str = Body(..., embed=True)) -> RockResponse[str]:
    await sandbox_manager.stop(sandbox_id)
    return RockResponse(result=f"{sandbox_id} stopped")


@sandbox_router.post("/commit")
@handle_exceptions(error_message="commit sandbox failed")
async def commit(
    sandbox_id: str = Body(..., embed=True),
    image_tag: str = Body(
        ...,
        embed=True,
        example="docker.io/library/nginx:1.25",
        description="commited image tag: <registry>/<repository>:<tag>",
    ),
    username: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
) -> RockResponse[str]:
    await sandbox_manager.commit(sandbox_id=sandbox_id, image_tag=image_tag, username=username, password=password)
    return RockResponse(result=f"{sandbox_id} commited")


@sandbox_router.post("/proxy/{sandbox_id}/{target_path:path}")
async def proxy_post(sandbox_id: str, target_path: str, request: Any = Body(...)):
    # TODO: impl
    result = {
        "sandbox_id": sandbox_id,
        "target_path": target_path,
        "request": request,
    }
    return RockResponse(result=result)


@sandbox_router.get("/proxy/{sandbox_id}/{target_path:path}")
async def proxy_get(sandbox_id: str, target_path: str):
    # TODO: impl
    result = {
        "sandbox_id": sandbox_id,
        "target_path": target_path,
    }
    return RockResponse(result=result)


@sandbox_router.get("/get_nacos_config")
async def get_nacos_config():
    await sandbox_manager.rock_config.update()
    return RockResponse(result=sandbox_manager.rock_config.sandbox_config)


@sandbox_router.get("/sandboxes/{id}/mount-infos")
async def get_mount(id: str):
    return RockResponse(result=await sandbox_manager.get_mount(id))
