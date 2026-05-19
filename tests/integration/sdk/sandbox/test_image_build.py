"""Integration tests for Image.from_dockerfile() → Sandbox.start() flow.

Verifies that a sandbox can be started from a local Dockerfile directory,
including build, cache skip, and content-change rebuild scenarios.

Run: pytest tests/integration/sdk/sandbox/test_image_build.py -v -m need_admin
"""

import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.image import Image
from rock.sdk.sandbox.image_builder import ImageBuilder
from rock.utils import ImageUtil

logger = init_logger(__name__)

TEST_DATA_DIR = Path(__file__).resolve().parents[2] / "test_data" / "image_from_dockerfile"
EXPECTED_FILE_CONTENT = "rock-image-from-dockerfile-ok"
MODIFIED_CONTENT = "rock-content-changed"


# ── Helpers ──


def _create_image(env_dir, registry_info, **kwargs):
    return Image.from_dockerfile(
        env_dir,
        image_name=registry_info["image_tag"],
        registry_username=registry_info["registry_username"],
        registry_password=registry_info["registry_password"],
        **kwargs,
    )


def _create_config(image, admin_remote_server, registry_info=None):
    """Build a SandboxConfig for the just-built image.

    `image` is the already-resolved tag string (we pre-build via _build_with_loopback_nat
    so the SDK's auto-resolve path inside Sandbox.start() isn't triggered here).
    `registry_info` carries the credentials admin needs to pull the image.
    """
    base_url = f"{admin_remote_server.endpoint}:{admin_remote_server.port}"
    kwargs = dict(image=image, memory="2g", cpus=1.0, startup_timeout=600, base_url=base_url)
    if registry_info:
        kwargs["registry_username"] = registry_info["registry_username"]
        kwargs["registry_password"] = registry_info["registry_password"]
    return SandboxConfig(**kwargs)


@asynccontextmanager
async def _run_sandbox(config):
    """Start a sandbox with default session, yield it, always stop on exit."""
    sandbox = Sandbox(config)
    try:
        await sandbox.start()
        await sandbox.create_session(CreateBashSessionRequest(session="default"))
        yield sandbox
    finally:
        try:
            await sandbox.stop()
        except Exception as e:
            logger.warning("Failed to stop sandbox: %s", e)


async def _assert_file_content(sandbox, expected):
    result = await sandbox.arun(cmd="cat /opt/hello.txt", session="default")
    assert result.output is not None
    assert result.output.strip() == expected


# ── Fixtures / helpers ──


async def _inject_loopback_nat(builder, port: int) -> None:
    """NAT 127.0.0.1:port → builder.host_ip:port inside the builder.

    The local_registry fixture serves on the host's loopback (`localhost:port`, i.e.
    127.0.0.1:port). That address falls in 127.0.0.0/8 which dockerd trusts as insecure
    by default, but from inside the builder (its own netns) 127.0.0.1 is the builder's
    own loopback with no listener. Three things make the loopback URL actually reach
    the host's docker-proxy:
      1. enable route_localnet (kernel default forbids routing 127.x off lo)
      2. OUTPUT DNAT      127.0.0.1:port → host_ip:port   (rewrite outgoing dst)
      3. POSTROUTING MASQUERADE for host_ip:port          (rewrite src so reply routes back)
    """
    host_ip = builder.host_ip
    cmd = (
        "echo 1 | tee /proc/sys/net/ipv4/conf/all/route_localnet "
        "/proc/sys/net/ipv4/conf/lo/route_localnet > /dev/null && "
        f"iptables -t nat -A OUTPUT -p tcp -d 127.0.0.1 --dport {port} "
        f"-j DNAT --to-destination {host_ip}:{port} && "
        f"iptables -t nat -A POSTROUTING -p tcp -d {host_ip} --dport {port} -j MASQUERADE"
    )
    logger.info("Injecting builder loopback NAT: 127.0.0.1:%s -> %s:%s", port, host_ip, port)
    obs = await builder.arun(cmd=cmd, session=ImageBuilder.BUILD_SESSION, mode="normal")
    if obs.exit_code != 0:
        raise RuntimeError(f"NAT setup failed (exit_code={obs.exit_code}): {obs.failure_reason or obs.output}")


async def _build_with_loopback_nat(image: Image, admin_remote_server) -> str:
    """Drive the build using a builder we own so we can inject test-only NAT.

    Returns the resolved image name (string) once build+push completes.
    """
    base_url = f"{admin_remote_server.endpoint}:{admin_remote_server.port}"
    image_builder = ImageBuilder(base_url=base_url, cluster="default")
    builder = image_builder.create_builder()
    await builder.start()
    try:
        await builder.create_session(CreateBashSessionRequest(session=ImageBuilder.BUILD_SESSION))
        registry, _ = ImageUtil.parse_registry_and_others(image.image_name)
        host_part, _, port_part = (registry or "").partition(":")
        if (host_part.startswith("127.") or host_part == "localhost") and port_part:
            await _inject_loopback_nat(builder, int(port_part))
        return await image_builder.build_with_builder(image, builder)
    finally:
        try:
            await builder.stop()
        except Exception:
            logger.warning("Failed to stop builder sandbox: %s", builder.sandbox_id, exc_info=True)


@pytest.fixture
def local_registry_info(local_registry):
    registry_url, username, password = local_registry
    return {
        "image_tag": f"{registry_url}/rock-test/image-from-dockerfile:latest",
        "registry_username": username,
        "registry_password": password,
    }


@pytest.fixture
def modified_env_dir(tmp_path):
    """Copy test data and modify hello.txt to detect rebuild."""
    env_dir = tmp_path / "env"
    shutil.copytree(TEST_DATA_DIR, env_dir)
    (env_dir / "hello.txt").write_text(MODIFIED_CONTENT + "\n")
    return env_dir


# ── Tests ──


@pytest.mark.need_admin
@pytest.mark.asyncio
async def test_from_dockerfile_build_and_start(local_registry_info, admin_remote_server):
    """Image.from_dockerfile() → build/push (via test-managed builder) → Sandbox.start()."""
    image = _create_image(TEST_DATA_DIR, local_registry_info)
    resolved = await _build_with_loopback_nat(image, admin_remote_server)
    config = _create_config(resolved, admin_remote_server, local_registry_info)
    async with _run_sandbox(config) as sandbox:
        await _assert_file_content(sandbox, EXPECTED_FILE_CONTENT)


@pytest.mark.need_admin
@pytest.mark.asyncio
async def test_from_dockerfile_cache_skip(local_registry_info, admin_remote_server):
    """Second build of the same Image should hit cache (CACHE_HIT) and skip push."""
    image = _create_image(TEST_DATA_DIR, local_registry_info)

    t0 = time.monotonic()
    resolved = await _build_with_loopback_nat(image, admin_remote_server)
    first_duration = time.monotonic() - t0

    t0 = time.monotonic()
    resolved2 = await _build_with_loopback_nat(image, admin_remote_server)
    second_duration = time.monotonic() - t0

    assert resolved == resolved2
    logger.info("First build: %.1fs, second build: %.1fs", first_duration, second_duration)
    assert second_duration < first_duration


@pytest.mark.need_admin
@pytest.mark.asyncio
async def test_from_dockerfile_rebuilds_on_content_change(local_registry_info, admin_remote_server, modified_env_dir):
    """Content change in env_dir triggers rebuild, new file content is picked up."""
    image = _create_image(modified_env_dir, local_registry_info)
    resolved = await _build_with_loopback_nat(image, admin_remote_server)
    config = _create_config(resolved, admin_remote_server, local_registry_info)
    async with _run_sandbox(config) as sandbox:
        await _assert_file_content(sandbox, MODIFIED_CONTENT)
