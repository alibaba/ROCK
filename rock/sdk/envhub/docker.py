"""Unified async Docker client combining regionless mirror resolution with Docker CLI operations."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import yaml

from rock.logger import init_logger
from rock.sdk.envhub.regionless.resolver import _DEFAULT_PROBE_TIMEOUT_SEC, RockRegistryResolver
from rock.utils.docker import DockerUtil, ImageUtil

logger = init_logger(__name__)


class DockerClient:
    """SDK entry point for regionless image resolution and Docker lifecycle operations.

    Regionless operations (resolve / rewrite) are delegated to
    :class:`RockRegistryResolver`; Docker CLI operations use
    :class:`DockerUtil` via ``asyncio.to_thread``.
    """

    def __init__(
        self,
        resolver: RockRegistryResolver | None = None,
        docker_executable: str = "docker",
        registries: list[str] | None = None,
    ) -> None:
        self._resolver = resolver or RockRegistryResolver(registries=registries)
        self._docker_executable = docker_executable

    # ------------------------------------------------------------------
    # Regionless: resolve
    # ------------------------------------------------------------------

    async def resolve_image(
        self,
        image: str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> str:
        """Resolve an image reference to a ROCK mirror if available."""
        return await self._resolver.resolve_image(image, timeout_sec=timeout_sec)

    async def resolve_dockerfile(
        self,
        dockerfile: Path | str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> bool:
        """Rewrite ``FROM`` images in a Dockerfile to ROCK mirrors when available."""
        return await self._resolver.resolve_dockerfile(Path(dockerfile), timeout_sec=timeout_sec)

    async def resolve_compose(
        self,
        compose_path: Path | str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> bool:
        """Rewrite ``image:`` fields in a compose file to ROCK mirrors when available."""
        return await self._resolve_compose_file(Path(compose_path), timeout_sec=timeout_sec)

    # ------------------------------------------------------------------
    # Regionless: resolve + pull
    # ------------------------------------------------------------------

    async def pull_image(
        self,
        image: str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> subprocess.CompletedProcess:
        """Resolve image to a ROCK mirror, then ``docker pull``.

        Resolution failures are non-blocking — falls back to pulling the
        original image.
        """
        try:
            resolved = await self.resolve_image(image, timeout_sec=timeout_sec)
        except Exception:
            logger.warning("Image resolution failed for %s, pulling original", image, exc_info=True)
            resolved = image

        proc = await asyncio.create_subprocess_exec(
            "docker",
            "pull",
            resolved,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        result = subprocess.CompletedProcess(
            args=["docker", "pull", resolved],
            returncode=proc.returncode,
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else "",
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"docker pull failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

        return result

    async def pull_compose(
        self,
        compose_path: Path | str,
        *,
        services: list[str] | None = None,
        project_name: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Resolve images in a compose file to ROCK mirrors, then ``docker compose pull``."""
        compose_path = Path(compose_path)

        try:
            await self._resolve_compose_file(compose_path, timeout_sec=timeout_sec)
        except Exception:
            logger.warning(
                "resolve_compose failed for %s, proceeding with original images", compose_path, exc_info=True
            )

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
                f"docker compose pull failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        return result

    # ------------------------------------------------------------------
    # Docker: authentication
    # ------------------------------------------------------------------

    async def login(self, registry: str, username: str, password: str, *, timeout: int = 30) -> str:
        return await asyncio.to_thread(DockerUtil.login, registry, username, password, timeout)

    async def logout(self, registry: str, *, timeout: int = 30) -> str:
        return await asyncio.to_thread(DockerUtil.logout, registry, timeout)

    # ------------------------------------------------------------------
    # Docker: build & push
    # ------------------------------------------------------------------

    async def build(
        self,
        dockerfile: str,
        context_path: str,
        tag: str,
        *extra_args: str,
    ) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(
            DockerUtil.buildx_build,
            dockerfile,
            context_path,
            "--tag",
            tag,
            *extra_args,
            docker_executable=self._docker_executable,
        )

    async def push(self, tag: str) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(DockerUtil.push_image_tag, tag, docker_executable=self._docker_executable)

    # ------------------------------------------------------------------
    # Docker: mirror (composite)
    # ------------------------------------------------------------------

    async def mirror(
        self,
        source_image: str,
        target_registry: str,
        *,
        target_username: str,
        target_password: str,
        source_registry: str | None = None,
        source_username: str | None = None,
        source_password: str | None = None,
    ) -> str:
        """Pull, re-tag, and push an image to a target registry.

        Returns the full target image reference that was pushed.
        """
        _, other_part = ImageUtil.parse_registry_and_others(source_image)
        parsed_ns, parsed_name, parsed_tag = ImageUtil.split_image_name(other_part)
        target_ref = f"{target_registry}/{parsed_ns}/{parsed_name}:{parsed_tag}"

        await self.login(target_registry, target_username, target_password)

        if source_username and source_password and source_registry:
            await self.login(source_registry, source_username, source_password)

        await self._pull(source_image)
        await self._tag(source_image, target_ref)
        await self.push(target_ref)

        logger.info("Mirrored %s -> %s", source_image, target_ref)
        return target_ref

    # ------------------------------------------------------------------
    # Private: Docker CLI wrappers
    # ------------------------------------------------------------------

    async def _pull(self, image: str) -> bytes:
        return await asyncio.to_thread(DockerUtil.pull_image, image)

    async def _tag(self, source: str, target: str) -> None:
        await asyncio.to_thread(DockerUtil.tag_image, source, target)

    async def _inspect(self, image: str) -> dict | None:
        return await asyncio.to_thread(DockerUtil.inspect_image, image)

    async def _is_image_available(self, image: str) -> bool:
        return await asyncio.to_thread(DockerUtil.is_image_available, image)

    async def _remove_image(self, image: str) -> bytes:
        return await asyncio.to_thread(DockerUtil.remove_image, image)

    # ------------------------------------------------------------------
    # Private: compose file resolution
    # ------------------------------------------------------------------

    async def _resolve_compose_file(
        self,
        compose_path: Path,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> bool:
        """Rewrite ``image:`` of every service in a compose file to ROCK mirrors when available."""
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
        for _svc_name, svc_config in services.items():
            if not isinstance(svc_config, dict):
                continue
            image = svc_config.get("image")
            if not isinstance(image, str) or not image:
                continue
            resolved = await self._resolver.resolve_image(image, timeout_sec=timeout_sec)
            if resolved != image:
                svc_config["image"] = resolved
                changed = True

        if changed:
            compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

        return changed
