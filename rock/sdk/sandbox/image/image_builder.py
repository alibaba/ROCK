from __future__ import annotations

import io
import logging
import os
import shlex
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.constants import BUILD_SCRIPT_TEMPLATE, DOCKERD_SCRIPT, PUSH_SCRIPT_TEMPLATE
from rock.sdk.sandbox.image.config import BuilderConfig, BuildSpec
from rock.utils import HttpUtils, ImageUtil

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)


class ImageBuilder:
    """Drive the DinD build + push for a BuildSpec inside a builder sandbox.

    Pure consumer of `BuildSpec` and `BuilderConfig`; does not depend on Image.
    """

    BUILD_SESSION = "build"

    def __init__(
        self,
        *,
        builder_config: BuilderConfig,
        builder: Sandbox | None = None,
    ):
        """Supplying `builder` skips builder-lifecycle management (caller owns
        start/stop) — used by tests that need to customise the builder
        environment (e.g. iptables NAT injection) before the build runs.
        """
        self._builder_config = builder_config
        self._builder = builder

    def create_builder(self) -> Sandbox:
        """Construct (but do not start) the builder sandbox from `builder_config`."""
        # Lazy import to avoid client → image → client cycle.
        from rock.sdk.sandbox.client import Sandbox

        return Sandbox(self._builder_config)

    async def build(self, spec: BuildSpec) -> str:
        """Build the image described by `spec`.

        Does a registry preflight first; on hit, skips builder creation entirely.
        """
        if await self._image_exists_in_registry(spec):
            logger.info("Image %s already exists in registry, skipping build", spec.image)
            return spec.image

        if self._builder is not None:
            return await self.build_with_builder(spec, self._builder)

        builder = self.create_builder()
        try:
            await builder.start()
            return await self.build_with_builder(spec, builder)
        finally:
            try:
                await builder.stop()
            except Exception:
                logger.warning("Failed to stop builder sandbox: %s", builder.sandbox_id, exc_info=True)

    async def _image_exists_in_registry(self, spec: BuildSpec) -> bool:
        """Fast-path HEAD on the registry manifest. Returns True only on 200.

        Any non-200 / network error returns False so the caller proceeds with a
        full build (safe default). No bearer-token challenge dance — registries
        that require it (e.g. Docker Hub) fall through to the in-builder cache
        check; private registries (ACR / Harbor) using Basic Auth work directly.
        """
        if spec.force_build:
            return False
        try:
            repo_with_registry, tag = spec.image.rsplit(":", 1)
            registry, repo = repo_with_registry.split("/", 1)
        except ValueError:
            return False

        host = registry.split(":", 1)[0]
        scheme = "http" if host in ("localhost", "127.0.0.1") else "https"
        url = f"{scheme}://{registry}/v2/{repo}/manifests/{tag}"

        auth = None
        if spec.registry_username and spec.registry_password:
            auth = (spec.registry_username, spec.registry_password)

        status = await HttpUtils.head(
            url,
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            auth=auth,
            timeout=5.0,
            verify=False,
        )
        return status == 200

    async def build_with_builder(self, spec: BuildSpec, builder: Sandbox) -> str:
        """Run dockerd → build → push against an already-started builder. Internal
        helper called by build(); skips registry preflight (build() did it).
        """
        session = self.BUILD_SESSION
        await builder.create_session(CreateBashSessionRequest(session=session))

        await self._run_script(builder, session, DOCKERD_SCRIPT, "/tmp/rock_dockerd.sh", "DOCKERD_OK", 120)

        # The build script does its own registry preflight from the builder's
        # network — covers the SDK-can't-reach-but-builder-can scenario (VPC
        # registry seen from a user's laptop).
        context_path = await self._upload_context(builder, session, spec)
        build_script = self._gen_build_script(spec, context_path)
        build_output = await self._run_script(builder, session, build_script, "/tmp/rock_build.sh", "BUILD_OK", 1800)
        if "CACHE_HIT" in build_output:
            logger.info("Image %s already exists (builder-side check), skipping push", spec.image)
            return spec.image

        push_script = self._gen_push_script(spec)
        await self._run_script(builder, session, push_script, "/tmp/rock_push.sh", "PUSH_OK", 600)

        logger.info("Successfully built and pushed image %s", spec.image)
        return spec.image

    async def _run_script(
        self, builder, session: str, script: str, remote_path: str, success_marker: str, timeout: int
    ) -> str:
        await builder.write_file_by_path(script, remote_path)
        obs = await builder.arun(cmd=f"bash {remote_path}", session=session, wait_timeout=timeout, mode="nohup")
        output = obs.output or ""
        if obs.exit_code != 0 or success_marker not in output:
            raise RuntimeError(f"Script {remote_path} failed (exit_code={obs.exit_code}): {output}")
        return output

    def _gen_build_script(self, spec: BuildSpec, context_path: str) -> str:
        build_arg_flags = " ".join(f"--build-arg {shlex.quote(f'{k}={v}')}" for k, v in spec.build_args.items())
        registry, _ = ImageUtil.parse_registry_and_others(spec.image)
        return BUILD_SCRIPT_TEMPLATE.format(
            image_name=shlex.quote(spec.image),
            content_hash=shlex.quote(spec.content_hash),
            registry=shlex.quote(registry or "docker.io"),
            registry_username=shlex.quote(spec.registry_username or ""),
            registry_password=shlex.quote(spec.registry_password or ""),
            force_build="true" if spec.force_build else "false",
            build_arg_flags=build_arg_flags,
            context_path=shlex.quote(context_path),
        )

    def _gen_push_script(self, spec: BuildSpec) -> str:
        registry, _ = ImageUtil.parse_registry_and_others(spec.image)
        return PUSH_SCRIPT_TEMPLATE.format(
            image_name=shlex.quote(spec.image),
            registry=shlex.quote(registry or "docker.io"),
            registry_username=shlex.quote(spec.registry_username or ""),
            registry_password=shlex.quote(spec.registry_password or ""),
        )

    async def _upload_context(self, builder, session: str, spec: BuildSpec) -> str:
        remote_tar = "/tmp/rock_env_dir.tar.gz"
        remote_ctx = "/tmp/rock_env_dir_ctx"

        src = Path(spec.dockerfile_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if src.is_file():
                # File mode: the single file IS the build context, packed as ./Dockerfile
                tar.add(src, arcname="Dockerfile")
            else:
                tar.add(src, arcname=".", filter=lambda ti: None if ti.name == ".git" else ti)
        tar_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(prefix="rock_env_dir_", suffix=".tar.gz", delete=False) as f:
            f.write(tar_bytes)
            local_tar_path = f.name
        try:
            upload_resp = await builder.upload_by_path(file_path=local_tar_path, target_path=remote_tar)
            if not upload_resp.success:
                raise RuntimeError(f"Failed to upload build context: {upload_resp.message}")
        finally:
            try:
                os.remove(local_tar_path)
            except OSError:
                pass

        await builder.arun(cmd=f"mkdir -p {remote_ctx}", session=session)
        await builder.arun(cmd=f"tar -xzf {remote_tar} -C {remote_ctx}", session=session)
        return remote_ctx
