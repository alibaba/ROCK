from __future__ import annotations

import io
import logging
import os
import shlex
import tarfile
import tempfile
from pathlib import Path

from rock import env_vars
from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.image import Image
from rock.utils import ImageUtil

logger = logging.getLogger(__name__)

_DOCKERD_SCRIPT = r"""#!/bin/bash
set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

if command -v dockerd &>/dev/null; then
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "DOCKERD_OK"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then
            echo "DOCKERD_FAIL"
            cat /var/log/dockerd.log 2>/dev/null | tail -50
            exit 1
        fi
    done
fi
"""

_BUILD_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

IMAGE_NAME={image_name}
CONTENT_HASH={content_hash}
FORCE_BUILD={force_build}

# ── Cache check ──
if [ "$FORCE_BUILD" != "true" ]; then
    if docker manifest inspect "$IMAGE_NAME" > /dev/null 2>&1; then
        docker pull "$IMAGE_NAME" > /dev/null 2>&1 || true
        REMOTE_HASH=$(docker inspect --format='{{{{index .Config.Labels "rock.content_hash"}}}}' "$IMAGE_NAME" 2>/dev/null || true)
        if [ "$REMOTE_HASH" = "$CONTENT_HASH" ]; then
            echo "CACHE_HIT"
            echo "BUILD_OK"
            exit 0
        else
            echo "Cache miss: content changed, rebuilding"
        fi
    fi
fi

# ── Docker build ──
echo "Building image $IMAGE_NAME..."
docker build {build_arg_flags} --label rock.content_hash="$CONTENT_HASH" -t "$IMAGE_NAME" {context_path}
echo "BUILD_OK"
"""

_PUSH_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

IMAGE_NAME={image_name}
REGISTRY={registry}
REG_USER={registry_username}
REG_PASS={registry_password}

# ── Registry login ──
if [ -n "$REG_USER" ] && [ -n "$REG_PASS" ]; then
    echo "$REG_PASS" | docker login "$REGISTRY" -u "$REG_USER" --password-stdin
else
    echo "No registry credentials, skipping login"
fi

# ── Docker push ──
echo "Pushing image $IMAGE_NAME..."
docker push "$IMAGE_NAME"
echo "PUSH_OK"
"""


class ImageBuilder:
    """将 Image 声明解析为镜像 tag 字符串。

    对于 base image 直接返回 tag。
    对于 dockerfile image，启动一个 builder sandbox 完成 DinD 构建和推送。
    """

    BUILD_SESSION = "build"

    def __init__(
        self,
        *,
        base_url: str,
        cluster: str,
        extra_headers: dict[str, str] | None = None,
        builder_image: str | None = None,
        _sandbox_factory=None,
    ):
        self._base_url = base_url
        self._cluster = cluster
        self._extra_headers = extra_headers or {}
        self._builder_image = builder_image
        self._sandbox_factory = _sandbox_factory

    def create_builder(self) -> Sandbox:
        """Construct (but do not start) the builder sandbox.

        Exposed so callers can start, customise (e.g. inject test-only NAT rules), then
        hand the running builder to :meth:`build_with_builder`.
        """
        builder_image = self._builder_image or env_vars.ROCK_IMAGE_BUILDER_IMAGE
        builder_cfg = SandboxConfig(
            image=builder_image,
            base_url=self._base_url,
            cluster=self._cluster,
            extra_headers=self._extra_headers,
            startup_timeout=600.0,
            auto_clear_seconds=60 * 30,
        )
        factory = self._sandbox_factory or Sandbox
        return factory(builder_cfg)

    async def build(self, image: Image) -> str:
        """Build `image` by managing the builder lifecycle internally."""
        if not image.needs_build:
            return image.image_name

        builder = self.create_builder()
        try:
            await builder.start()
            return await self.build_with_builder(image, builder)
        finally:
            try:
                await builder.stop()
            except Exception:
                logger.warning("Failed to stop builder sandbox: %s", builder.sandbox_id, exc_info=True)

    async def build_with_builder(self, image: Image, builder: Sandbox) -> str:
        """Run the build/push pipeline against an externally-managed, already-started
        builder sandbox.

        The caller owns `builder`'s lifecycle (start/stop) and is free to perform any
        environment-specific setup (firewall rules, mounts, etc.) before calling this.
        """
        if not image.needs_build:
            return image.image_name

        session = self.BUILD_SESSION
        await builder.create_session(CreateBashSessionRequest(session=session))

        # ── Phase 1: Start dockerd ──
        await self._run_script(builder, session, _DOCKERD_SCRIPT, "/tmp/rock_dockerd.sh", "DOCKERD_OK", 120)

        # ── Phase 2: Build image ──
        content_hash = image.content_hash()
        context_path = await self._upload_context(builder, session, image)
        build_script = self._gen_build_script(image, content_hash, context_path)
        build_output = await self._run_script(builder, session, build_script, "/tmp/rock_build.sh", "BUILD_OK", 600)
        if "CACHE_HIT" in build_output:
            logger.info("Image %s cache hit, skipping push", image.image_name)
            return image.image_name

        # ── Phase 3: Login and push ──
        push_script = self._gen_push_script(image)
        await self._run_script(builder, session, push_script, "/tmp/rock_push.sh", "PUSH_OK", 300)

        logger.info("Successfully built and pushed image %s", image.image_name)
        return image.image_name

    async def _run_script(
        self, builder, session: str, script: str, remote_path: str, success_marker: str, timeout: int
    ) -> str:
        await builder.write_file_by_path(script, remote_path)
        obs = await builder.arun(cmd=f"bash {remote_path}", session=session, wait_timeout=timeout, mode="nohup")
        output = obs.output or ""
        if obs.exit_code != 0 or success_marker not in output:
            raise RuntimeError(f"Script {remote_path} failed (exit_code={obs.exit_code}): {output}")
        return output

    def _gen_build_script(self, image: Image, content_hash: str, context_path: str) -> str:
        build_arg_flags = " ".join(f"--build-arg {shlex.quote(f'{k}={v}')}" for k, v in image.build_args.items())
        return _BUILD_SCRIPT_TEMPLATE.format(
            image_name=shlex.quote(image.image_name),
            content_hash=shlex.quote(content_hash),
            force_build="true" if image.force_build else "false",
            build_arg_flags=build_arg_flags,
            context_path=shlex.quote(context_path),
        )

    def _gen_push_script(self, image: Image) -> str:
        registry, _ = ImageUtil.parse_registry_and_others(image.image_name)
        return _PUSH_SCRIPT_TEMPLATE.format(
            image_name=shlex.quote(image.image_name),
            registry=shlex.quote(registry or "docker.io"),
            registry_username=shlex.quote(image.registry_username or ""),
            registry_password=shlex.quote(image.registry_password or ""),
        )

    async def _upload_context(self, builder, session: str, image: Image) -> str:
        remote_tar = "/tmp/rock_env_dir.tar.gz"
        remote_ctx = "/tmp/rock_env_dir_ctx"

        env_dir = Path(image.dockerfile_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(env_dir, arcname=".", filter=lambda ti: None if ti.name == ".git" else ti)
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
