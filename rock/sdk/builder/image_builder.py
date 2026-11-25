"""Build and push a Docker image from a local env_dir (Dockerfile context).
"""

import io
import logging
import os
import shlex
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from rock import env_vars
from rock.actions import CreateBashSessionRequest
from rock.sdk.builder.base import EnvBuilder
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.utils import ImageUtil

logger = logging.getLogger(__name__)

REMOTE_TAR = "/tmp/rock_env_dir.tar.gz"
REMOTE_CTX = "/tmp/rock_env_dir_ctx"
REMOTE_PWD_PATH = "/tmp/rock_registry_password"

@runtime_checkable
class DockerBuildExecutor(Protocol):
    """Executor for docker build steps: same sequence (context, build, login, push)."""

    async def prepare_context(self, env_dir: str | Path) -> str:
        """Prepare build context; return path to use for 'docker build'."""
        ...

    async def run_shell(self, cmd: str) -> None:
        """Run a shell command (remote run_in_session)."""
        ...

    async def upload_secret(self, content: bytes) -> str:
        """Upload secret (e.g. registry password); return path for use in run_shell (e.g. cat path | docker login)."""
        ...

class BuilderSandboxExecutor(DockerBuildExecutor):
    """Run docker build steps inside a builder sandbox."""

    def __init__(
        self,
        builder: Sandbox,
        session: str,
    ):
        self._builder = builder
        self._session = session

    @staticmethod
    def _pack_env_dir_to_tar_gz(env_dir: str | Path) -> bytes:
        buf = io.BytesIO()
        env_dir = Path(env_dir).resolve()
        if not env_dir.is_dir():
            raise ValueError(f"env_dir is not a directory: {env_dir}")
        dockerfile = env_dir / "Dockerfile"
        if not dockerfile.exists():
            raise ValueError(f"Dockerfile not found in env_dir: {dockerfile}")
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(env_dir, arcname=".", filter=lambda ti: None if ti.name == ".git" else ti)
        return buf.getvalue()

    async def prepare_context(self, env_dir: str | Path) -> str:
        # Start dockerd in background (same as base.py image mirror flow)
        logger.info("Starting dockerd in builder sandbox...")
        await self._builder.arun(cmd="service docker start", session=self._session, mode="normal")

        tar_bytes = self._pack_env_dir_to_tar_gz(env_dir)
        logger.info("Uploading build context (%d bytes) to %s", len(tar_bytes), REMOTE_TAR)
        local_tar_path = None
        with tempfile.NamedTemporaryFile(prefix="rock_env_dir_", suffix=".tar.gz", delete=False) as f:
            f.write(tar_bytes)
            local_tar_path = f.name
        try:
            upload_resp = await self._builder.upload_by_path(file_path=local_tar_path, target_path=REMOTE_TAR)
            if not upload_resp.success:
                raise RuntimeError(f"Failed to upload build context: {upload_resp.message}")

            await self._builder.arun(cmd=f"mkdir -p {REMOTE_CTX}", session=self._session, mode="normal")
            await self._builder.arun(cmd=f"tar -xzf {REMOTE_TAR} -C {REMOTE_CTX}", session=self._session, mode="normal")

            return REMOTE_CTX
        finally:
            try:
                os.remove(local_tar_path)
            except OSError:
                pass

    async def run_shell(self, cmd: str) -> None:
        obs = await self._builder.arun(cmd=cmd, session=self._session, mode="normal")
        if obs.exit_code != 0:
            raise RuntimeError(f"Command failed (exit_code={obs.exit_code}): {obs.failure_reason or obs.output}")

    async def upload_secret(self, content: bytes) -> str:
        with tempfile.NamedTemporaryFile(prefix="rock_registry_pwd_", delete=False) as f:
            f.write(content)
            local_pwd_path = f.name
        try:
            pwd_upload = await self._builder.upload_by_path(file_path=local_pwd_path, target_path=REMOTE_PWD_PATH)
            if not pwd_upload.success:
                raise RuntimeError(f"Failed to upload registry password: {pwd_upload.message}")
            return REMOTE_PWD_PATH
        finally:
            try:
                os.remove(local_pwd_path)
            except OSError:
                pass

class ImageBuilder(EnvBuilder):
    """Build a Docker image from a local env_dir (Dockerfile context) and push to a remote registry.

    Example usage:
        builder = ImageBuilder()
        image_tag = await builder.build(
            instance_record={"env_dir": "/path/to/env_dir", "image_tag": "myreg.io/myimg:tag"},
            base_url="http://localhost:8080",
            registry_username="user",
            registry_password="pass",
        )
    """
    async def build_with_builder_sandbox(
        self,
        base_url: str | None = None,
        auth_token: str | None = None,
        cluster: str | None = None,
        registry_username: str | None = None,
        registry_password: str | None = None,
        builder_image: str | None = None,
        **kwargs,
    ) -> str:
        """Start one builder sandbox, run docker build via BuilderSandboxExecutor, then stop. Returns image_tag."""
        builder_image = builder_image or await self.get_env_build_image()
        builder_cfg = SandboxConfig(
            extra_headers=({"XRL-Authorization": auth_token} if auth_token else {}),
            image=builder_image,
            cluster=cluster or "default",
            registry_username=registry_username,
            registry_password=registry_password,
            startup_timeout=600.0,
        )
        if base_url:
            builder_cfg.base_url = base_url
        builder = Sandbox(builder_cfg)
        session = "default"
        env_dir = kwargs.get("env_dir")
        image_tag = (kwargs.get("image_tag") or "").strip()
        registry_username = kwargs.get("registry_username") or registry_username
        registry_password = kwargs.get("registry_password") or registry_password
        if not env_dir or not image_tag:
            raise ValueError("env_dir and image_tag are required")
        try:
            await builder.start()
            await builder.create_session(CreateBashSessionRequest(session=session))
            executor = BuilderSandboxExecutor(builder, session)
            context_path = await executor.prepare_context(env_dir)
            await executor.run_shell(
                f"docker build -t {shlex.quote(image_tag)} {shlex.quote(context_path)}",
            )
            if registry_username and registry_password:
                registry, _ = ImageUtil.parse_registry_and_others(image_tag)
                if not registry:
                    registry = "docker.io"
                registry_arg = f" {shlex.quote(registry)}"
                secret_path = await executor.upload_secret(registry_password.encode())
                await executor.run_shell(
                    f"cat {shlex.quote(secret_path)} | docker login{registry_arg} -u {shlex.quote(registry_username)} --password-stdin",
                )
            await executor.run_shell(f"docker push {shlex.quote(image_tag)}")
            return image_tag
        finally:
            try:
                await builder.stop()
            except Exception:
                logger.warning("Failed to stop builder sandbox: %s", builder.sandbox_id, exc_info=True)

    async def build(
        self,
        instance_record: dict[str, str] | None = None,
        *,
        base_url: str | None = None,
        auth_token: str | None = None,
        cluster: str | None = None,
        registry_username: str | None = None,
        registry_password: str | None = None,
        builder_image: str | None = None,
        **kwargs,
    ) -> str:
        record = instance_record or {}

        env_dir = record.get("env_dir")
        if not env_dir:
            raise ValueError("env_dir is required in instance_record")
        env_dir_path = Path(env_dir)
        if not env_dir_path.is_dir():
            raise ValueError(f"env_dir is not a directory: {env_dir}")
        if not (env_dir_path / "Dockerfile").exists():
            raise ValueError(f"Dockerfile not found in env_dir: {env_dir_path / 'Dockerfile'}")

        image_tag = (record.get("image_tag") or "").strip()
        if not image_tag:
            raise ValueError("image_tag is required in instance_record")

        logger.info("ImageBuilder starting build for %s from %s", image_tag, env_dir)
        return await self.build_with_builder_sandbox(
            base_url=base_url,
            auth_token=auth_token,
            cluster=cluster,
            registry_username=registry_username,
            registry_password=registry_password,
            builder_image=builder_image,
            env_dir=env_dir,
            image_tag=image_tag,
        )

    async def get_env_build_image(self) -> str:
        return env_vars.ROCK_IMAGE_BUILDER_IMAGE

    async def verify(self, **kwargs):
        pass