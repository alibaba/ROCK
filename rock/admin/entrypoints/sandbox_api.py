import math
import re
import time
from typing import Annotated, Any

import httpx
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
    SANDBOX_DISK_OVERCOMMIT_RATIO_KEY,
    SUPPORT_KATA_SWITCH,
)
from rock.common.exception import handle_exceptions
from rock.common.validation import NonBlankStr
from rock.config import ImageRegistryMirror
from rock.deployments.config import AcceleratorType, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.docker import ImageUtil

logger = init_logger(__name__)

_MIRROR_PROBE_CACHE: dict[str, tuple[bool, float]] = {}
_MIRROR_PROBE_TTL_SECONDS = 60.0

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
    """Apply disk limits with priority: user request > Nacos > RuntimeConfig > None.

    The log dir shares the rootfs prjid + bhard at runtime, so only rootfs is configurable.

    When an overcommit ratio (> 1.0) is configured, it is stored on
    ``config.disk_overcommit_ratio`` so Ray scheduling can request
    ``disk / ratio`` resources while Docker uses the full ``disk`` value.
    """
    runtime = sandbox_manager.rock_config.runtime
    nacos = sandbox_manager.rock_config.nacos_provider

    if config.disk is None:
        nacos_rootfs = await nacos.get_config_value(SANDBOX_DISK_LIMIT_ROOTFS_KEY) if nacos else None
        config.disk = nacos_rootfs or runtime.sandbox_disk_limit_rootfs

    if config.disk is not None:
        nacos_ratio_str = await nacos.get_config_value(SANDBOX_DISK_OVERCOMMIT_RATIO_KEY) if nacos else None
        ratio = float(nacos_ratio_str) if nacos_ratio_str else runtime.sandbox_disk_overcommit_ratio
        if ratio is not None and ratio > 1.0:
            config.disk_overcommit_ratio = ratio


def _probe_cache_get(candidate: str) -> bool | None:
    entry = _MIRROR_PROBE_CACHE.get(candidate)
    if entry is None:
        return None
    hit, expires_at = entry
    if expires_at < time.monotonic():
        _MIRROR_PROBE_CACHE.pop(candidate, None)
        return None
    return hit


def _probe_cache_set(candidate: str, hit: bool) -> None:
    _MIRROR_PROBE_CACHE[candidate] = (hit, time.monotonic() + _MIRROR_PROBE_TTL_SECONDS)


def _apply_mirror_hit(config: DockerDeploymentConfig, mirror, candidate: str) -> None:
    config.image = candidate
    config.registry_username = mirror.username
    config.registry_password = mirror.password


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    """Parse ``realm``, ``service``, ``scope`` from a Bearer WWW-Authenticate header."""
    return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)="([^"]*)"', header)}


async def _http_probe_manifest(
    registry: str,
    repo: str,
    tag: str,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 5,
) -> bool:
    """Check whether ``repo:tag`` exists on *registry* via the v2 manifest API."""
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    headers = {
        "Accept": ", ".join([
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json",
        ])
    }
    auth = (username, password) if username and password else None

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers, auth=auth)

        if resp.status_code == 401 and "www-authenticate" in resp.headers:
            www_auth = resp.headers["www-authenticate"]
            if www_auth.startswith("Bearer "):
                params = _parse_bearer_challenge(www_auth)
                realm = params.get("realm", "")
                service = params.get("service", "")
                scope = params.get("scope", "")
                token_url = f"{realm}?service={service}&scope={scope}"
                token_resp = await client.get(token_url, auth=auth)
                if token_resp.status_code == 200:
                    data = token_resp.json()
                    token = data.get("token") or data.get("access_token")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        resp = await client.get(url, headers=headers)

        return resp.status_code == 200


async def _apply_image_registry_mirror(config: DockerDeploymentConfig) -> None:
    """Rewrite ``config.image`` to an internal mirror copy when one exists.

    Gated by ``rock_config.image_mirror_lookup_allowlist`` (opt-in): empty list
    disables the lookup for every image; ``["*"]`` enables it for all; otherwise
    only images whose full string starts with one of the listed prefixes are
    considered.

    For allowed images, iterates ``rock_config.image_registry_mirrors`` in
    declared order, probes via the registry v2 manifest API and uses the first
    hit. Digest references (``name@sha256:...``) are skipped entirely.
    """
    nacos = sandbox_manager.rock_config.nacos_provider
    nacos_cfg = (await nacos.get_config() or {}) if nacos else {}

    allowlist = nacos_cfg.get(
        "image_mirror_lookup_allowlist",
        sandbox_manager.rock_config.image_mirror_lookup_allowlist,
    )
    if not allowlist:
        return
    if "*" not in allowlist and not any(config.image.startswith(p) for p in allowlist):
        return

    raw_mirrors = nacos_cfg.get("image_registry_mirrors")
    if raw_mirrors is not None:
        mirrors = [ImageRegistryMirror(**m) for m in raw_mirrors]
    else:
        mirrors = sandbox_manager.rock_config.image_registry_mirrors
    if not mirrors:
        return

    _, repo_and_tag = ImageUtil.parse_registry_and_others(config.image)
    if "/" in repo_and_tag:
        original_namespace, name_tag = repo_and_tag.split("/", 1)
    else:
        original_namespace = None
        name_tag = repo_and_tag
    if "@" in name_tag:
        logger.info(
            f"image registry mirror skip for digest reference {config.image!r} "
            "(content-addressed, mirror replacement would change semantics)"
        )
        return
    if ":" not in name_tag:
        name_tag = f"{name_tag}:{ImageUtil.DEFAULT_TAG}"
    original_image = config.image

    image_name, tag = name_tag.rsplit(":", 1)
    for mirror in mirrors:
        if not mirror.registry or not mirror.namespace:
            continue

        candidates = []
        if original_namespace:
            candidates.append((f"{mirror.registry}/{original_namespace}/{name_tag}", f"{original_namespace}/{image_name}"))
        if original_namespace != mirror.namespace:
            candidates.append((f"{mirror.registry}/{mirror.namespace}/{name_tag}", f"{mirror.namespace}/{image_name}"))

        for candidate, repo in candidates:
            cached = _probe_cache_get(candidate)
            if cached is True:
                logger.info(f"image registry mirror hit (cached): {original_image!r} -> {candidate!r}")
                _apply_mirror_hit(config, mirror, candidate)
                return
            if cached is False:
                continue

            try:
                hit = await _http_probe_manifest(
                    registry=mirror.registry,
                    repo=repo,
                    tag=tag,
                    username=mirror.username,
                    password=mirror.password,
                )
            except Exception as e:
                logger.warning(f"image registry mirror probe failed for {candidate!r}: {e}")
                continue

            _probe_cache_set(candidate, hit)
            if hit:
                logger.info(f"image registry mirror hit: {original_image!r} -> {candidate!r}")
                _apply_mirror_hit(config, mirror, candidate)
                return
    logger.info(f"image registry mirror miss for {original_image!r}, keep original")

async def _apply_timeout_defaults(config: DockerDeploymentConfig) -> None:
    """Apply startup_timeout default, min and max from SandboxLifecycleConfig (YAML + Nacos).

    startup_timeout covers docker pull + wait-until-alive combined.
    - config.startup_timeout is None: SDK didn't set it, apply lifecycle.default_startup_timeout_seconds.
    - config.startup_timeout is set: keep the user value.
    - In all cases: clamp to [min_startup_timeout_seconds, max_startup_timeout_seconds].
    Nacos updates lifecycle via RockConfig.update() called in DeploymentManager.init_config().
    """
    lifecycle = sandbox_manager.rock_config.lifecycle
    if config.startup_timeout is None:
        config.startup_timeout = lifecycle.default_startup_timeout_seconds
    config.startup_timeout = max(config.startup_timeout, lifecycle.min_startup_timeout_seconds)
    config.startup_timeout = min(config.startup_timeout, lifecycle.max_startup_timeout_seconds)


async def _apply_image_os_profile(config: DockerDeploymentConfig) -> None:
    """Look up an image_os_profile by ``config.image_os`` and apply it.

    Profile sources are merged in priority order:
      1. rock YAML (runtime.image_os_profiles) — base config
      2. Nacos image_os_profiles — incremental override (same key replaces whole profile)
    The profile whose key equals ``config.image_os`` is selected.

    Side-effects when a profile matches:
    - config.image_os_profile is set (used by DockerDeployment to instantiate
      ConfigurableRuntimeEnv instead of the ROCK_WORKER_ENV_TYPE fallback)
    - config.startup_timeout is updated if the profile declares one and the SDK didn't set it
    """
    profiles: dict = {}
    yaml_profiles = getattr(sandbox_manager.rock_config.runtime, "image_os_profiles", None)
    if yaml_profiles:
        profiles.update(yaml_profiles)

    nacos = sandbox_manager.rock_config.nacos_provider
    if nacos is not None:
        nacos_config = await nacos.get_config() or {}
        nacos_profiles = nacos_config.get("image_os_profiles", {})
        if isinstance(nacos_profiles, dict):
            profiles.update(nacos_profiles)

    data = profiles.get(config.image_os)
    if not isinstance(data, dict):
        return

    config.image_os_profile = {"name": config.image_os, **data}

    profile_timeout = data.get("startup_timeout")
    if profile_timeout and config.startup_timeout is None:
        config.startup_timeout = float(profile_timeout)

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
    await _apply_image_os_profile(config)
    await _apply_timeout_defaults(config)
    await _apply_disk_limits(config)
    await _apply_image_registry_mirror(config)
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
    await _apply_image_os_profile(config)
    await _apply_timeout_defaults(config)
    await _apply_cpu_overcommit_default(config, headers.user_info.get("rock_authorization"))
    await _apply_disk_limits(config)
    await _apply_image_registry_mirror(config)
    sandbox_start_response = await sandbox_manager.start_async(
        config,
        user_info=headers.user_info,
        cluster_info=headers.cluster_info,
    )
    return RockResponse(result=sandbox_start_response)


@sandbox_router.get("/is_alive")
@handle_exceptions(error_message="get sandbox is alive failed")
async def is_alive(sandbox_id: NonBlankStr):
    try:
        status_response = await sandbox_manager.get_status(sandbox_id)
        alive_response = IsAliveResponse(is_alive=status_response.is_alive, message=status_response.host_name)
        return RockResponse(result=alive_response)
    except Exception:
        false_response = IsAliveResponse(is_alive=False, message=f"sandbox {sandbox_id} is alive failed")
        return RockResponse(result=false_response)


@sandbox_router.get("/get_sandbox_statistics")
@handle_exceptions(error_message="get sandbox statistics failed")
async def get_sandbox_statistics(sandbox_id: NonBlankStr):
    return RockResponse(result=await sandbox_manager.get_sandbox_statistics(sandbox_id))


@sandbox_router.get("/get_status")
@handle_exceptions(error_message="get sandbox status failed")
async def get_status(sandbox_id: NonBlankStr, include_all_states: bool = False):
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
    sandbox_id: Annotated[NonBlankStr, Form()] = ...,
) -> RockResponse[UploadResponse]:
    return RockResponse(result=await sandbox_manager.upload(file, target_path, sandbox_id))


@sandbox_router.post("/stop")
@handle_exceptions(error_message="stop sandbox failed")
async def close(sandbox_id: Annotated[NonBlankStr, Body(embed=True)]) -> RockResponse[str]:
    await sandbox_manager.stop(sandbox_id)
    return RockResponse(result=f"{sandbox_id} stopped")


@sandbox_router.post("/delete")
@handle_exceptions(error_message="delete sandbox failed")
async def delete(sandbox_id: str = Body(..., embed=True)) -> RockResponse:
    """Soft-delete a stopped sandbox.

    Returns 400-equivalent (status=Failed) when the sandbox is not in ``stopped``
    state. Unknown sandbox is idempotent (Success). After this call the DB
    record holds ``state='deleted'`` and the worker container has been removed
    via ``operator.delete``.

    Return type is the bare ``RockResponse`` (not ``RockResponse[str]``) so the
    ``handle_exceptions`` decorator can fill ``result`` with a ``SandboxResponse``
    on the failure path without tripping FastAPI's response_model validation.
    """
    await sandbox_manager.delete(sandbox_id)
    return RockResponse(result=f"{sandbox_id} deleted")


@sandbox_router.post("/restart")
@handle_exceptions(error_message="restart sandbox failed")
async def restart(sandbox_id: str = Body(..., embed=True)) -> RockResponse[SandboxStartResponse]:
    result = await sandbox_manager.restart_async(sandbox_id)
    return RockResponse(result=result)


@sandbox_router.post("/sandboxes/{sandbox_id}/restart")
@handle_exceptions(error_message="restart sandbox failed")
async def restart_restful(sandbox_id: NonBlankStr) -> RockResponse[SandboxStartResponse]:
    result = await sandbox_manager.restart_async(sandbox_id)
    return RockResponse(result=result)


@sandbox_router.post("/commit")
@handle_exceptions(error_message="commit sandbox failed")
async def commit(
    sandbox_id: Annotated[NonBlankStr, Body(embed=True)],
    image_tag: Annotated[
        NonBlankStr,
        Body(
            embed=True,
            example="docker.io/library/nginx:1.25",
            description="commited image tag: <registry>/<repository>:<tag>",
        ),
    ],
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
