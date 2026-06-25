"""Compose-level regionless support: resolve images in compose files and pull."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import yaml

from rock.logger import init_logger
from rock.sdk.envhub.regionless.resolver import _DEFAULT_PROBE_TIMEOUT_SEC, RockRegistryResolver

logger = init_logger(__name__)

_default_resolver = RockRegistryResolver()


async def resolve_compose(
    compose_path: Path,
    *,
    timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    resolver: RockRegistryResolver | None = None,
) -> bool:
    """Rewrite ``image:`` of every service in a compose file to ROCK mirrors when available.

    Only rewrites ``image:`` fields; ``build:`` sections are left untouched.
    Returns True if any service image was rewritten.
    """
    r = resolver or _default_resolver

    try:
        text = compose_path.read_text()
        data = yaml.safe_load(text)
    except Exception:
        logger.warning("Failed to parse compose file %s, skipping regionless rewrite", compose_path, exc_info=True)
        return False

    if not isinstance(data, dict):
        return False

    services = data.get("services")
    if not isinstance(services, dict):
        return False

    changed = False
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        image = svc_config.get("image")
        if not isinstance(image, str) or not image:
            continue
        resolved = await r.resolve_image(image, timeout_sec=timeout_sec)
        if resolved != image:
            svc_config["image"] = resolved
            changed = True

    if changed:
        compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    return changed


async def compose_pull(
    compose_path: Path,
    *,
    services: list[str] | None = None,
    project_name: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    extra_args: list[str] | None = None,
    resolver: RockRegistryResolver | None = None,
) -> subprocess.CompletedProcess:
    """Resolve images in *compose_path* to ROCK mirrors, then ``docker compose pull``.

    1. resolve_compose(compose_path) — rewrite service images to regional mirrors
    2. docker compose -f <compose_path> [-p <project>] pull [services...]

    Resolve failures are non-blocking (fail-safe). Pull failures raise RuntimeError.
    """
    try:
        await resolve_compose(compose_path, timeout_sec=timeout_sec, resolver=resolver)
    except Exception:
        logger.warning("resolve_compose failed for %s, proceeding with original images", compose_path, exc_info=True)

    cmd = ["docker", "compose", "-f", str(compose_path)]
    if project_name:
        cmd.extend(["-p", project_name])
    cmd.append("pull")
    if extra_args:
        cmd.extend(extra_args)
    if services:
        cmd.extend(services)

    run_env = dict(os.environ)
    if env:
        run_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()

    result = subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else "",
        stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else "",
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose pull failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    return result
