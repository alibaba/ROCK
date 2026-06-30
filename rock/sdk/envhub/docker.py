"""Facade composing regionless mirror resolution with general Docker operations."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from rock.logger import init_logger
from rock.sdk.envhub.docker_ops import DockerOps
from rock.sdk.envhub.regionless.compose import compose_pull, resolve_compose
from rock.sdk.envhub.regionless.resolver import _DEFAULT_PROBE_TIMEOUT_SEC, RockRegistryResolver

logger = init_logger(__name__)


class DockerFacade:
    """Unified SDK entry point combining regionless image resolution with
    general Docker lifecycle operations.

    Regionless operations (resolve / rewrite) are delegated to
    :class:`RockRegistryResolver`; plain Docker operations (login, build,
    push, tag, …) are delegated to :class:`DockerOps`.
    """

    def __init__(
        self,
        resolver: RockRegistryResolver | None = None,
        docker_executable: str = "docker",
        registries: list[str] | None = None,
    ) -> None:
        """
        Args:
            resolver: Custom resolver instance. When provided, *registries* is
                ignored (the resolver's own configuration takes precedence).
            docker_executable: Path to the docker CLI binary.
            registries: Explicit list of mirror registries (``host/namespace``).
                Passed to :class:`RockRegistryResolver` when no custom
                *resolver* is given.  *None* (default) falls back to the
                ``INSTANCE_ROCK_REGISTRY`` environment variable.
        """
        self._resolver = resolver or RockRegistryResolver(registries=registries)
        self._ops = DockerOps(docker_executable=docker_executable)

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
        return await resolve_compose(Path(compose_path), timeout_sec=timeout_sec, resolver=self._resolver)

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
        return await compose_pull(
            Path(compose_path),
            services=services,
            project_name=project_name,
            env=env,
            timeout_sec=timeout_sec,
            extra_args=extra_args,
            resolver=self._resolver,
        )

    # ------------------------------------------------------------------
    # General Docker operations (delegated to DockerOps)
    # ------------------------------------------------------------------

    async def login(self, registry: str, username: str, password: str, *, timeout: int = 30) -> str:
        return await self._ops.login(registry, username, password, timeout=timeout)

    async def logout(self, registry: str, *, timeout: int = 30) -> str:
        return await self._ops.logout(registry, timeout=timeout)

    async def build(
        self,
        dockerfile: str,
        context_path: str,
        tag: str,
        *extra_args: str,
    ) -> subprocess.CompletedProcess:
        return await self._ops.build(dockerfile, context_path, tag, *extra_args)

    async def push(self, tag: str) -> subprocess.CompletedProcess:
        return await self._ops.push(tag)

    async def tag(self, source: str, target: str) -> None:
        await self._ops.tag(source, target)

    async def inspect(self, image: str) -> dict | None:
        return await self._ops.inspect(image)

    async def is_image_available(self, image: str) -> bool:
        return await self._ops.is_image_available(image)

    async def remove_image(self, image: str) -> bytes:
        return await self._ops.remove_image(image)

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
        return await self._ops.mirror(
            source_image,
            target_registry,
            target_username=target_username,
            target_password=target_password,
            source_registry=source_registry,
            source_username=source_username,
            source_password=source_password,
        )
