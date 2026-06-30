"""Async wrapper for general-purpose Docker CLI operations.

Delegates to :class:`~rock.utils.docker.DockerUtil` and
:class:`~rock.sdk.builder.provider.docker.DockerCommand` via
``asyncio.to_thread``, adding no logic of its own.
"""

from __future__ import annotations

import asyncio
import subprocess

from rock.logger import init_logger
from rock.sdk.builder.provider.docker import DockerCommand
from rock.utils.docker import DockerUtil, ImageUtil

logger = init_logger(__name__)


class DockerOps:
    """Async interface for Docker CLI operations.

    Every method is a thin async wrapper around an existing synchronous
    utility.  No regionless / mirror logic lives here — that belongs in
    :class:`~rock.sdk.envhub.docker.DockerFacade`.
    """

    def __init__(self, docker_executable: str = "docker") -> None:
        self._docker_cmd = DockerCommand(docker_executable=docker_executable)
        self._docker_executable = docker_executable

    # ------------------------------------------------------------------
    # Registry authentication
    # ------------------------------------------------------------------

    async def login(self, registry: str, username: str, password: str, *, timeout: int = 30) -> str:
        return await asyncio.to_thread(DockerUtil.login, registry, username, password, timeout)

    async def logout(self, registry: str, *, timeout: int = 30) -> str:
        return await asyncio.to_thread(DockerUtil.logout, registry, timeout)

    # ------------------------------------------------------------------
    # Pull (plain, no resolve)
    # ------------------------------------------------------------------

    async def pull(self, image: str) -> bytes:
        return await asyncio.to_thread(DockerUtil.pull_image, image)

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
        return await asyncio.to_thread(
            self._docker_cmd.buildx_build, dockerfile, context_path, "--tag", tag, *extra_args
        )

    async def push(self, tag: str) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(self._docker_cmd.push_image, tag)

    async def tag(self, source: str, target: str) -> None:
        await asyncio.to_thread(DockerUtil.tag_image, source, target)

    # ------------------------------------------------------------------
    # Inspect & query
    # ------------------------------------------------------------------

    async def inspect(self, image: str) -> dict | None:
        return await asyncio.to_thread(DockerUtil.inspect_image, image)

    async def is_image_available(self, image: str) -> bool:
        return await asyncio.to_thread(DockerUtil.is_image_available, image)

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    async def remove_image(self, image: str) -> bytes:
        return await asyncio.to_thread(DockerUtil.remove_image, image)

    # ------------------------------------------------------------------
    # Mirror (composite)
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

        await self.pull(source_image)
        await self.tag(source_image, target_ref)
        await self.push(target_ref)

        logger.info("Mirrored %s -> %s", source_image, target_ref)
        return target_ref
