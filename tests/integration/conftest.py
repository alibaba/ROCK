import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest
import uvicorn
from fastapi.testclient import TestClient

from rock import env_vars
from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.utils import find_free_port, run_until_complete
from rock.utils.concurrent_helper import run_until_complete
from rock.utils.docker import DockerUtil
from rock.utils.system import find_free_port
from tests.conftest import start_rocklet_process

logger = init_logger(__name__)

SKIP_IF_NO_DOCKER = pytest.mark.skipif(
    not (DockerUtil.is_docker_available() and DockerUtil.is_image_available(env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE)),
    reason=f"Requires Docker and image {env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE}",
)

SKIP_IF_NO_STORAGE_OPT = pytest.mark.skipif(
    not DockerUtil.detect_storage_opt_support(),
    reason="Requires Docker with storage-opt support (overlay2 + xfs + prjquota/pquota)",
)

_log_path = env_vars.ROCK_LOGGING_PATH or ""

SKIP_IF_LOG_PATH_NOT_XFS = pytest.mark.skipif(
    not _log_path or not DockerUtil.is_xfs_prjquota_path(_log_path),
    reason=f"ROCK_LOGGING_PATH ({_log_path!r}) is not set or not on XFS with prjquota, skipping log quota test",
)


@dataclass
class RemoteServer:
    port: int
    endpoint: str = "http://127.0.0.1"


## Rocklet Client & Remote Server


@pytest.fixture(scope="session")
def rocklet_test_client():
    from rock.rocklet.server import app

    client = TestClient(app)
    yield client


@pytest.fixture(scope="session")
def rocklet_remote_server() -> RemoteServer:
    port = run_until_complete(find_free_port())
    from rock.rocklet.server import app

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    # Wait for the server to start
    max_retries = 10
    retry_delay = 0.1
    for _ in range(max_retries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except (TimeoutError, ConnectionRefusedError):
            time.sleep(retry_delay)
    else:
        pytest.fail("Server did not start within the expected time")

    return RemoteServer(port)


## Admin Client & Remote Server


@pytest.fixture(name="admin_client", scope="session")
def admin_client_fixture():
    """Create test client using TestClient.

    The admin runs in-process via TestClient, but sandbox status probing
    (get_remote_status) posts to ``host_ip:ROCK_WORKER_ROCKLET_PORT``. Start a
    local rocklet process (like admin_remote_server) so those calls reach a live
    rocklet instead of failing with ConnectError.
    """
    # create_app()/lifespan read env+role from env vars (not argv). Use env=local
    # (ray operator) + role=admin so the gem service gets wired up.
    overrides = {"ROCK_ADMIN_ENV": "local", "ROCK_ADMIN_ROLE": "admin"}
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        with start_rocklet_process():
            from rock.admin.main import create_app

            # create_app() already wires up gem_router (see _include_routers)
            app = create_app()
            with TestClient(app) as client:
                yield client
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="session")
def admin_remote_server():
    external_base_url = os.getenv("ROCK_TEST_ADMIN_BASE_URL")
    if external_base_url:
        # Accept either "localhost:8080" or "http://localhost:8080".
        normalized = external_base_url if "://" in external_base_url else f"http://{external_base_url}"
        parsed = urlparse(normalized)
        if not parsed.hostname or not parsed.port:
            raise ValueError(
                f"Invalid ROCK_TEST_ADMIN_BASE_URL. Expected host:port or http://host:port, got: {external_base_url!r}"
            )
        logger.info("Using external admin server from ROCK_TEST_ADMIN_BASE_URL=%s", external_base_url)
        yield RemoteServer(port=parsed.port, endpoint=f"{parsed.scheme}://{parsed.hostname}")
        return

    port = run_until_complete(find_free_port())

    # start_rocklet_process exports ROCK_WORKER_ROCKLET_PORT, which the admin
    # subprocess inherits via os.environ.copy() below.
    with start_rocklet_process():
        env = os.environ.copy()
        # Do not redirect stdout and stderr to pipes without reading from them, as this will cause the program to hang.
        process = subprocess.Popen(
            [
                "admin",
                "--env",
                "test",
                "--role",
                "admin",
                "--port",
                str(port),
            ],
            stdout=None,
            stderr=None,
            env=env,
        )

        # Wait for the admin server to start (rocklet is already up).
        max_retries = 10
        retry_delay = 3
        for _ in range(max_retries):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except (TimeoutError, ConnectionRefusedError):
                time.sleep(retry_delay)
        else:
            process.kill()
            pytest.fail("Server did not start within the expected time")

        logger.info(f"Admin server started on port {port}")
        try:
            yield RemoteServer(port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
def sandbox_config(admin_remote_server):
    config = SandboxConfig(
        image="python:3.11",
        memory="8g",
        cpus=2.0,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
    )
    return config


@pytest.fixture
async def sandbox_instance(request, admin_remote_server):
    """Provides an independent sandbox instance for each test case, ensuring stop is always called after failure"""
    # Get the image parameter from the test function, use default value if not present
    image = getattr(request, "param", {}).get("image", "python:3.11")
    cpus = getattr(request, "param", {}).get("cpus", 0.5)
    memory = getattr(request, "param", {}).get("memory", "1g")

    config = SandboxConfig(
        image=image, memory=memory, cpus=cpus, base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}"
    )
    sandbox = Sandbox(config)
    try:
        await sandbox.start()
        logger.info(f"sandbox started with image {image}, {repr(sandbox)}")
        await sandbox.create_session(CreateBashSessionRequest(session="default"))
        yield sandbox
    finally:
        # Stop will be executed regardless of whether the test succeeds or fails
        try:
            await sandbox.stop()
            logger.info(f"sandbox stopped, {str(sandbox)}")
        except Exception as e:
            logger.warning(f"Failed to stop sandbox: {e}")


def get_test_data(relative_path: str) -> str:
    project_root = Path(__file__).parent
    data_dir = project_root / "test_data"
    file_path = data_dir / relative_path
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return file_path.absolute().as_posix()


@pytest.fixture()
async def local_registry():
    """Start a local Docker registry with basic auth"""
    auth_dir = Path(tempfile.mkdtemp())
    htpasswd_file = auth_dir / "htpasswd"
    container_name = "test-registry"

    # 1. Generate htpasswd file
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "htpasswd", "httpd:2", "-Bbn", "testuser", "testpass"],
        capture_output=True,
        text=True,
        check=True,
    )
    htpasswd_file.write_text(result.stdout)

    # 2. Remove any leftover container from a previous failed run
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # 3. Start registry and mount the htpasswd file into the container
    port = run_until_complete(find_free_port())
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{port}:5000",
            "-v",
            f"{htpasswd_file}:/auth/htpasswd",
            "-e",
            "REGISTRY_AUTH=htpasswd",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_REALM=Registry Realm",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
            "registry:2",
        ],
        check=True,
    )

    # 4. Wait for registry to be ready
    registry_url = f"localhost:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://{registry_url}/v2/", timeout=1)
            break
        except urllib.error.HTTPError as http_err:
            # 401 means the registry is up but requires authentication — that's expected
            if http_err.code == 401:
                break
            time.sleep(0.5)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    else:
        raise RuntimeError(f"Registry at {registry_url} did not become ready in time")

    yield registry_url, "testuser", "testpass"

    # 5. Cleanup
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    htpasswd_file.unlink(missing_ok=True)
    auth_dir.rmdir()


@pytest.fixture(scope="session")
def local_minio():
    """Start a local MinIO server for archive storage tests."""
    container_name = "test-minio-archive"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    port = run_until_complete(find_free_port())
    console_port = run_until_complete(find_free_port())
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{port}:9000",
            "-p",
            f"{console_port}:9001",
            "-e",
            "MINIO_ROOT_USER=rockadmin",
            "-e",
            "MINIO_ROOT_PASSWORD=rockadmin123",
            "minio/minio:RELEASE.2024-12-18T13-15-44Z",
            "server",
            "/data",
            "--console-address",
            ":9001",
        ],
        check=True,
    )

    endpoint = f"http://localhost:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{endpoint}/minio/health/live", timeout=1)
            break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    else:
        raise RuntimeError(f"MinIO at {endpoint} did not become ready")

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="rockadmin",
        aws_secret_access_key="rockadmin123",
        region_name="us-east-1",
    )
    s3.create_bucket(Bucket="rock-archive-test")

    yield endpoint, "rockadmin", "rockadmin123", "rock-archive-test"

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


@pytest.fixture(scope="session")
def local_snapshot_registry():
    """Start a local Docker registry with basic auth and delete support."""
    container_name = "test-snapshot-registry"
    username, password = "testuser", "testpass"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    port = run_until_complete(find_free_port())

    auth_dir = tempfile.mkdtemp()
    htpasswd_file = os.path.join(auth_dir, "htpasswd")
    result = subprocess.run(
        ["docker", "run", "--rm", "httpd:2", "htpasswd", "-Bbn", username, password],
        capture_output=True,
        text=True,
    )
    with open(htpasswd_file, "w") as f:
        f.write(result.stdout)

    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{port}:5000",
            "-v",
            f"{htpasswd_file}:/auth/htpasswd",
            "-e",
            "REGISTRY_AUTH=htpasswd",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_REALM=Registry Realm",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
            "-e",
            "REGISTRY_STORAGE_DELETE_ENABLED=true",
            "registry:2",
        ],
        check=True,
    )

    registry_url = f"localhost:{port}"
    for _ in range(30):
        try:
            req = urllib.request.Request(f"http://{registry_url}/v2/")
            import base64

            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {credentials}")
            r = urllib.request.urlopen(req, timeout=1)
            if r.status == 200:
                break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    else:
        raise RuntimeError(f"Registry at {registry_url} did not become ready")

    yield registry_url, username, password

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    import shutil

    shutil.rmtree(auth_dir, ignore_errors=True)
