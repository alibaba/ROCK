"""Facade for Docker operations with regionless mirror support."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from rock.logger import init_logger
from rock.sdk.builder.provider.docker import DockerCommand
from rock.sdk.envhub.regionless.compose import compose_pull, resolve_compose
from rock.sdk.envhub.regionless.resolver import _DEFAULT_PROBE_TIMEOUT_SEC, RockRegistryResolver
from rock.utils.docker import DockerUtil, ImageUtil

logger = init_logger(__name__)


class DockerFacade:
    """Unified SDK entry point for Docker operations with ROCK mirror registry support.

    Aggregates regionless image resolution, Dockerfile rewriting, compose
    file handling, and general Docker lifecycle operations (login, build,
    push, tag, inspect, remove, mirror) behind a single async facade.
    """

    def __init__(
        self,
        resolver: RockRegistryResolver | None = None,
        docker_executable: str = "docker",
    ) -> None:
        self._resolver = resolver or RockRegistryResolver()
        self._docker_cmd = DockerCommand(docker_executable=docker_executable)
        self._docker_executable = docker_executable

    async def resolve_image(
        self,
        image: str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> str:
        """Resolve an image reference to a ROCK mirror if available."""
        return await self._resolver.resolve_image(image, timeout_sec=timeout_sec)

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
    # Registry authentication
    # ------------------------------------------------------------------

    async def login(self, registry: str, username: str, password: str, *, timeout: int = 30) -> str:
        """Authenticate to a Docker registry."""
        return await asyncio.to_thread(DockerUtil.login, registry, username, password, timeout)

    async def logout(self, registry: str, *, timeout: int = 30) -> str:
        """Logout from a Docker registry."""
        return await asyncio.to_thread(DockerUtil.logout, registry, timeout)

    # ------------------------------------------------------------------
    # Build & push
    # ------------------------------------------------------------------

    async def build(
        self,
        dockerfile: str,
        context_path: str,
        tag: str,
        *extra_args: str,
    ) -> subprocess.CompletedProcess:
        """Run ``docker buildx build``."""
        return await asyncio.to_thread(
            self._docker_cmd.buildx_build, dockerfile, context_path, "--tag", tag, *extra_args
        )

    async def push(self, tag: str) -> subprocess.CompletedProcess:
        """Push an image to its registry."""
        return await asyncio.to_thread(self._docker_cmd.push_image, tag)

    async def tag(self, source: str, target: str) -> subprocess.CompletedProcess:
        """Tag a local image with a new name."""
        proc = await asyncio.create_subprocess_exec(
            self._docker_executable, "tag", source, target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result = subprocess.CompletedProcess(
            args=[self._docker_executable, "tag", source, target],
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace") if stdout else "",
            stderr=stderr.decode(errors="replace") if stderr else "",
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker tag failed (exit {result.returncode}): {result.stderr}")
        return result

    # ------------------------------------------------------------------
    # Inspect & query
    # ------------------------------------------------------------------

    async def inspect(self, image: str) -> dict | None:
        """Return parsed ``docker inspect`` output, or *None* if the image is not found locally."""
        proc = await asyncio.create_subprocess_exec(
            self._docker_executable, "inspect", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode(errors="replace"))
        return data[0] if isinstance(data, list) and data else data

    async def is_image_available(self, image: str) -> bool:
        """Check whether an image exists in the local Docker cache."""
        return await asyncio.to_thread(DockerUtil.is_image_available, image)

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    async def remove_image(self, image: str) -> subprocess.CompletedProcess:
        """Remove a local Docker image."""
        proc = await asyncio.create_subprocess_exec(
            self._docker_executable, "rmi", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result = subprocess.CompletedProcess(
            args=[self._docker_executable, "rmi", image],
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace") if stdout else "",
            stderr=stderr.decode(errors="replace") if stderr else "",
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker rmi failed (exit {result.returncode}): {result.stderr}")
        return result

    # ------------------------------------------------------------------
    # Mirror (composite operation)
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
        """Pull an image, re-tag it to a target registry, and push.

        Returns the full target image reference that was pushed.
        """
        _, other_part = ImageUtil.parse_registry_and_others(source_image)
        parsed_ns, parsed_name, parsed_tag = ImageUtil.split_image_name(other_part)
        target_ref = f"{target_registry}/{parsed_ns}/{parsed_name}:{parsed_tag}"

        await self.login(target_registry, target_username, target_password)

        if source_username and source_password and source_registry:
            await self.login(source_registry, source_username, source_password)

        await self.pull_image(source_image)
        await self.tag(source_image, target_ref)
        await self.push(target_ref)

        logger.info("Mirrored %s -> %s", source_image, target_ref)
        return target_ref
