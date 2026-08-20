"""Shared sandbox start configuration normalization."""

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable

from rock.common.constants import (
    CPU_OVERCOMMIT_ALLOWED_KEYS_KEY,
    CPU_OVERCOMMIT_HEADROOM_KEY,
    EXTRA_ACCELERATOR_TYPES_KEY,
    KATA_DIND_DISK_SIZE_KEY,
    KATA_RUNTIME_SWITCH,
    SANDBOX_DISK_LIMIT_ROOTFS_KEY,
    SANDBOX_DISK_OVERCOMMIT_RATIO_KEY,
    SUPPORT_KATA_SWITCH,
)
from rock.config import ImageRegistryMirror, RockConfig
from rock.deployments.config import AcceleratorType, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.docker import ImageUtil

logger = init_logger(__name__)

MIRROR_PROBE_CACHE: dict[str, tuple[bool, float]] = {}
_MIRROR_PROBE_TTL_SECONDS = 60.0
_MIRROR_PROBE_CACHE_MAX_ENTRIES = 4096
_MIRROR_PROBE_INFLIGHT: dict[str, asyncio.Task[bool]] = {}

ProbeManifest = Callable[..., Awaitable[bool]]


async def apply_kata_runtime_switch(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Check Nacos switches and enable kata runtime when supported."""
    nacos = rock_config.nacos_provider
    if nacos is not None and await nacos.get_switch_status(SUPPORT_KATA_SWITCH, False):
        config.use_kata_runtime = await nacos.get_switch_status(KATA_RUNTIME_SWITCH, False) or config.use_kata_runtime
    else:
        config.use_kata_runtime = False


async def apply_kata_disk_size(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Override the kata DinD disk size from Nacos when configured."""
    if not config.use_kata_runtime:
        return
    nacos = rock_config.nacos_provider
    if nacos is not None:
        disk_size = await nacos.get_config_value(KATA_DIND_DISK_SIZE_KEY)
        if disk_size:
            config.kata_disk_size = disk_size


async def apply_disk_limits(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Apply disk limit and overcommit defaults from Nacos/runtime config."""
    runtime = rock_config.runtime
    nacos = rock_config.nacos_provider

    if config.disk is None:
        nacos_rootfs = await nacos.get_config_value(SANDBOX_DISK_LIMIT_ROOTFS_KEY) if nacos else None
        config.disk = nacos_rootfs or runtime.sandbox_disk_limit_rootfs

    if config.disk is not None:
        nacos_ratio_str = await nacos.get_config_value(SANDBOX_DISK_OVERCOMMIT_RATIO_KEY) if nacos else None
        ratio = float(nacos_ratio_str) if nacos_ratio_str else runtime.sandbox_disk_overcommit_ratio
        if ratio is not None and ratio > 1.0:
            config.disk_overcommit_ratio = ratio


def _probe_cache_get(candidate: str) -> bool | None:
    entry = MIRROR_PROBE_CACHE.pop(candidate, None)
    if entry is None:
        return None
    hit, expires_at = entry
    if expires_at < time.monotonic():
        return None
    MIRROR_PROBE_CACHE[candidate] = entry
    return hit


def _probe_cache_set(candidate: str, hit: bool) -> None:
    MIRROR_PROBE_CACHE.pop(candidate, None)
    if len(MIRROR_PROBE_CACHE) >= _MIRROR_PROBE_CACHE_MAX_ENTRIES:
        MIRROR_PROBE_CACHE.pop(next(iter(MIRROR_PROBE_CACHE)))
    MIRROR_PROBE_CACHE[candidate] = (hit, time.monotonic() + _MIRROR_PROBE_TTL_SECONDS)


def _finish_probe(candidate: str, task: asyncio.Task[bool]) -> None:
    if _MIRROR_PROBE_INFLIGHT.get(candidate) is task:
        _MIRROR_PROBE_INFLIGHT.pop(candidate, None)
    if task.cancelled() or task.exception() is not None:
        return
    _probe_cache_set(candidate, task.result())


async def _probe_manifest_singleflight(
    candidate: str,
    probe_manifest: ProbeManifest,
    **probe_args,
) -> bool:
    cached = _probe_cache_get(candidate)
    if cached is not None:
        return cached

    task = _MIRROR_PROBE_INFLIGHT.get(candidate)
    if task is None:
        task = asyncio.create_task(probe_manifest(**probe_args))
        _MIRROR_PROBE_INFLIGHT[candidate] = task
        task.add_done_callback(lambda completed: _finish_probe(candidate, completed))
    return await asyncio.shield(task)


def _apply_mirror_hit(config: DockerDeploymentConfig, mirror: ImageRegistryMirror, candidate: str) -> None:
    config.image = candidate
    config.registry_username = mirror.username
    config.registry_password = mirror.password


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    """Parse ``realm``, ``service`` and ``scope`` from a Bearer challenge."""
    return {match.group(1): match.group(2) for match in re.finditer(r'(\w+)="([^"]*)"', header)}


async def http_probe_manifest(
    rock_config: RockConfig,
    registry: str,
    repo: str,
    tag: str,
    username: str | None = None,
    password: str | None = None,
) -> bool:
    """Check whether ``repo:tag`` exists on a registry via the v2 API."""
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    headers = {
        "Accept": ", ".join(
            [
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.index.v1+json",
            ]
        )
    }
    auth = (username, password) if username and password else None

    client = rock_config.http_pool_manager.get("probe")
    response = await client.get(url, headers=headers, auth=auth)

    if response.status_code == 401 and "www-authenticate" in response.headers:
        challenge = response.headers["www-authenticate"]
        if challenge.startswith("Bearer "):
            params = _parse_bearer_challenge(challenge)
            realm = params.get("realm", "")
            service = params.get("service", "")
            scope = params.get("scope", "")
            token_url = f"{realm}?service={service}&scope={scope}"
            token_response = await client.get(token_url, auth=auth)
            if token_response.status_code == 200:
                data = token_response.json()
                token = data.get("token") or data.get("access_token")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    response = await client.get(url, headers=headers)

    return response.status_code == 200


async def apply_image_registry_mirror(
    rock_config: RockConfig,
    config: DockerDeploymentConfig,
    *,
    probe_manifest: ProbeManifest | None = None,
) -> None:
    """Rewrite ``config.image`` to the first available configured mirror."""
    nacos = rock_config.nacos_provider
    nacos_config = (await nacos.get_config() or {}) if nacos else {}

    allowlist = nacos_config.get(
        "image_mirror_lookup_allowlist",
        rock_config.image_mirror_lookup_allowlist,
    )
    if not allowlist:
        return
    if "*" not in allowlist and not any(config.image.startswith(prefix) for prefix in allowlist):
        return

    raw_mirrors = nacos_config.get("image_registry_mirrors")
    mirrors = (
        [ImageRegistryMirror(**mirror) for mirror in raw_mirrors]
        if raw_mirrors is not None
        else rock_config.image_registry_mirrors
    )
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

    if probe_manifest is None:

        async def probe_manifest(**kwargs) -> bool:
            return await http_probe_manifest(rock_config, **kwargs)

    image_name, tag = name_tag.rsplit(":", 1)
    for mirror in mirrors:
        if not mirror.registry or not mirror.namespace:
            continue

        candidates = []
        if original_namespace:
            candidates.append(
                (f"{mirror.registry}/{original_namespace}/{name_tag}", f"{original_namespace}/{image_name}")
            )
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
                hit = await _probe_manifest_singleflight(
                    candidate,
                    probe_manifest,
                    registry=mirror.registry,
                    repo=repo,
                    tag=tag,
                    username=mirror.username,
                    password=mirror.password,
                )
            except Exception as error:
                logger.warning(f"image registry mirror probe failed for {candidate!r}: {error}")
                continue

            if hit:
                logger.info(f"image registry mirror hit: {original_image!r} -> {candidate!r}")
                _apply_mirror_hit(config, mirror, candidate)
                return
    logger.info(f"image registry mirror miss for {original_image!r}, keep original")


async def apply_timeout_defaults(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Apply startup timeout default and min/max bounds."""
    lifecycle = rock_config.lifecycle
    if config.startup_timeout is None:
        config.startup_timeout = lifecycle.default_startup_timeout_seconds
    config.startup_timeout = max(config.startup_timeout, lifecycle.min_startup_timeout_seconds)
    config.startup_timeout = min(config.startup_timeout, lifecycle.max_startup_timeout_seconds)


async def apply_image_os_profile(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Apply the merged YAML/Nacos profile selected by ``config.image_os``."""
    profiles: dict = {}
    yaml_profiles = getattr(rock_config.runtime, "image_os_profiles", None)
    if yaml_profiles:
        profiles.update(yaml_profiles)

    nacos = rock_config.nacos_provider
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


async def apply_accelerator_type_validation(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Validate accelerator type against built-in and Nacos-provided values."""
    if config.accelerator_type is None:
        return

    allowed: set[str] = {item.value for item in AcceleratorType}
    nacos = rock_config.nacos_provider
    if nacos is not None:
        nacos_config = await nacos.get_config() or {}
        extras = nacos_config.get(EXTRA_ACCELERATOR_TYPES_KEY) or []
        if isinstance(extras, list):
            allowed.update(str(item) for item in extras)

    if config.accelerator_type not in allowed:
        raise BadRequestRockError(
            f"Invalid accelerator_type {config.accelerator_type!r}. Allowed values: {sorted(allowed)}"
        )


async def apply_cpu_overcommit_default(
    rock_config: RockConfig,
    config: DockerDeploymentConfig,
    rock_authorization: str | None,
) -> None:
    """Derive ``limit_cpus`` from Nacos headroom when the caller did not set it."""
    if config.limit_cpus is not None:
        return

    nacos = rock_config.nacos_provider
    if nacos is None:
        return

    nacos_config = await nacos.get_config() or {}
    allowed_keys = nacos_config.get(CPU_OVERCOMMIT_ALLOWED_KEYS_KEY)
    if allowed_keys is not None and (not isinstance(allowed_keys, list) or rock_authorization not in allowed_keys):
        return

    raw_headroom = nacos_config.get(CPU_OVERCOMMIT_HEADROOM_KEY)
    try:
        headroom = float(raw_headroom) if raw_headroom is not None else 0.0
    except (TypeError, ValueError):
        headroom = 0.0

    if not math.isfinite(headroom) or headroom <= 0:
        return

    config.limit_cpus = min(2 * config.cpus, config.cpus + headroom)


def apply_auto_clear_default(rock_config: RockConfig, config: DockerDeploymentConfig) -> None:
    """Use lifecycle auto-clear when the caller did not explicitly set it."""
    if "auto_clear_time_minutes" not in config.model_fields_set:
        default_seconds = rock_config.lifecycle.auto_transition.auto_clear_seconds
        config.auto_clear_time_minutes = default_seconds // 60


async def apply_start_config(
    rock_config: RockConfig,
    config: DockerDeploymentConfig,
    rock_authorization: str | None,
) -> None:
    """Apply the shared sandbox start normalization pipeline in dependency order."""
    apply_auto_clear_default(rock_config, config)
    await apply_accelerator_type_validation(rock_config, config)
    await apply_kata_runtime_switch(rock_config, config)
    await apply_kata_disk_size(rock_config, config)
    await apply_image_os_profile(rock_config, config)
    await apply_timeout_defaults(rock_config, config)
    await apply_cpu_overcommit_default(rock_config, config, rock_authorization)
    await apply_disk_limits(rock_config, config)
    await apply_image_registry_mirror(rock_config, config)
