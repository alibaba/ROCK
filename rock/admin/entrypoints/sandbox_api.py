import math
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
from rock.common.constants import (
    CPU_OVERCOMMIT_ALLOWED_KEYS_KEY,
    CPU_OVERCOMMIT_HEADROOM_KEY,
    EXTRA_ACCELERATOR_TYPES_KEY,
    GET_STATUS_SWITCH,
    KATA_DIND_DISK_SIZE_KEY,
    KATA_RUNTIME_SWITCH,
    SANDBOX_DISK_LIMIT_ROOTFS_KEY,
    SUPPORT_KATA_SWITCH,
)
from rock.common.exception import handle_exceptions
from rock.deployments.config import AcceleratorType, DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError

sandbox_router = APIRouter()
sandbox_manager: SandboxManager


def set_sandbox_manager(service: SandboxManager):
    global sandbox_manager
    sandbox_manager = service


async def _apply_kata_runtime_switch(config: DockerDeploymentConfig) -> None:
    """Check nacos switch and enable kata runtime on the config if the switch is on."""
    if (
        sandbox_manager.rock_config.nacos_provider is not None
        and await sandbox_manager.rock_config.nacos_provider.get_switch_status(SUPPORT_KATA_SWITCH, False)
    ):
        config.use_kata_runtime = (
            await sandbox_manager.rock_config.nacos_provider.get_switch_status(KATA_RUNTIME_SWITCH, False)
            or config.use_kata_runtime
        )
    else:
        config.use_kata_runtime = False


async def _apply_kata_disk_size(config: DockerDeploymentConfig) -> None:
    """Read kata_dind_disk_size from nacos and override config.kata_disk_size if present."""
    if not config.use_kata_runtime:
        return
    if sandbox_manager.rock_config.nacos_provider is not None:
        disk_size = await sandbox_manager.rock_config.nacos_provider.get_config_value(KATA_DIND_DISK_SIZE_KEY)
        if disk_size:
            config.kata_disk_size = disk_size


async def _apply_disk_limits(config: DockerDeploymentConfig) -> None:
    """Apply disk limits from RuntimeConfig (rock-xxx.yml), overridable by Nacos at runtime.

    Priority: Nacos > RuntimeConfig (rock-xxx.yml). None in both means no limit.
    The log dir shares the rootfs prjid + bhard at runtime, so only rootfs is configurable.
    """
    runtime = sandbox_manager.rock_config.runtime
    nacos = sandbox_manager.rock_config.nacos_provider

    disk_limit_rootfs = runtime.sandbox_disk_limit_rootfs

    if nacos is not None:
        nacos_rootfs = await nacos.get_config_value(SANDBOX_DISK_LIMIT_ROOTFS_KEY)
        if nacos_rootfs:
            disk_limit_rootfs = nacos_rootfs

    config.disk_limit_rootfs = disk_limit_rootfs


async def _apply_accelerator_type_validation(config: DockerDeploymentConfig) -> None:
    """Validate ``config.accelerator_type`` against the built-in enum union with
    Nacos-provided extras.

    Allowed set = ``AcceleratorType`` enum values ∪ list under Nacos key
    ``extra_accelerator_types``. When Nacos is unavailable or the key is missing,
    only the built-in enum applies. Raises :class:`BadRequestRockError` on
    mismatch. ``None`` is always allowed (caller did not request a specific GPU).
    """
    if config.accelerator_type is None:
        return

    allowed: set[str] = {item.value for item in AcceleratorType}

    nacos = sandbox_manager.rock_config.nacos_provider
    if nacos is not None:
        nacos_config = await nacos.get_config() or {}
        extras = nacos_config.get(EXTRA_ACCELERATOR_TYPES_KEY) or []
        if isinstance(extras, list):
            allowed.update(str(item) for item in extras)

    if config.accelerator_type not in allowed:
        raise BadRequestRockError(
            f"Invalid accelerator_type {config.accelerator_type!r}. " f"Allowed values: {sorted(allowed)}"
        )


async def _apply_cpu_overcommit_default(config: DockerDeploymentConfig, rock_authorization: str | None) -> None:
    """Derive limit_cpus from cpus + Nacos headroom when SDK did not set it.

    Formula: limit_cpus = min(2 * cpus, cpus + headroom)
    - SDK-supplied limit_cpus always wins (function is a no-op in that case).
    - Grayscale gate driven by Nacos list `cpu_overcommit_allowed_keys`:
      * key absent from Nacos -> gate is open for every caller (full rollout).
      * key present as a list  -> only `rock_authorization` values in the list pass.
      * key present but not a list (misconfigured) -> gate closed.
    - headroom is read from Nacos key `cpu_overcommit_headroom` (default 0).
    - headroom <= 0 keeps limit_cpus = None (docker run gets no --cpus flag).
    """
    if config.limit_cpus is not None:
        return

    nacos = sandbox_manager.rock_config.nacos_provider
    if nacos is None:
        return

    nacos_config = await nacos.get_config() or {}
    allowed_keys = nacos_config.get(CPU_OVERCOMMIT_ALLOWED_KEYS_KEY)
    if allowed_keys is not None and (not isinstance(allowed_keys, list) or rock_authorization not in allowed_keys):
        return

    raw = nacos_config.get(CPU_OVERCOMMIT_HEADROOM_KEY)
    try:
        headroom = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        headroom = 0.0

    # Reject NaN / inf so a fat-fingered Nacos edit can't break sandbox startup
    if not math.isfinite(headroom) or headroom <= 0:
        return

    config.limit_cpus = min(2 * config.cpus, config.cpus + headroom)


@sandbox_router.post("/start")
@handle_exceptions(error_message="start sandbox failed")
async def start(request: SandboxStartRequest) -> RockResponse[SandboxStartResponse]:
    config = DockerDeploymentConfig.from_request(request)
    await _apply_accelerator_type_validation(config)
    await _apply_kata_runtime_switch(config)
    await _apply_kata_disk_size(config)
    await _apply_disk_limits(config)
    sandbox_start_response = await sandbox_manager.start(config)
    return RockResponse(result=sandbox_start_response)


@sandbox_router.post("/start_async")
@handle_exceptions(error_message="async start sandbox failed")
async def start_async(
    request: SandboxStartRequest,
    headers: Annotated[StartHeaders, Depends()],
) -> RockResponse[SandboxStartResponse]:
    config = DockerDeploymentConfig.from_request(request)
    await _apply_accelerator_type_validation(config)
    await _apply_kata_runtime_switch(config)
    await _apply_kata_disk_size(config)
    await _apply_cpu_overcommit_default(config, headers.user_info.get("rock_authorization"))
    await _apply_disk_limits(config)
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
async def get_status(sandbox_id: str, include_all_states: bool = False):
    # TODO: do judgement inside operator
    if (
        sandbox_manager.rock_config.nacos_provider is not None
        and await sandbox_manager.rock_config.nacos_provider.get_switch_status(GET_STATUS_SWITCH)
    ):
        return RockResponse(
            result=await sandbox_manager.get_status_v2(sandbox_id, include_all_states=include_all_states)
        )
    return RockResponse(result=await sandbox_manager.get_status(sandbox_id, include_all_states=include_all_states))


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
