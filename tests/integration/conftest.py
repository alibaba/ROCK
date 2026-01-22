import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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

logger = init_logger(__name__)

SKIP_IF_NO_DOCKER = pytest.mark.skipif(
    not (DockerUtil.is_docker_available() and DockerUtil.is_image_available(env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE)),
    reason=f"Requires Docker and image {env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE}",
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
    """Create test client using TestClient"""
    # Save original sys.argv
    original_argv = sys.argv.copy()
    # Modify sys.argv
    sys.argv = ["main.py", "--env", "local", "--role", "admin"]
    try:
        from rock.admin.main import app, gem_router

        # Register env router
        app.include_router(gem_router, prefix="/apis/v1/envs/gem", tags=["gem"])
        with TestClient(app) as client:
            yield client
    finally:
        # Restore original sys.argv
        sys.argv = original_argv


@pytest.fixture(scope="session")
def admin_remote_server():
    port = run_until_complete(find_free_port())
    proxy_port = run_until_complete(find_free_port())

    env = os.environ.copy()
    env["ROCK_WORKER_ROCKLET_PORT"] = str(proxy_port)
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

    rocklet_process = subprocess.Popen(
        [
            "rocklet",
            "--port",
            str(proxy_port),
        ],
        stdout=None,
        stderr=None,
    )

    # Wait for the server to start
    max_retries = 10
    retry_delay = 3
    for _ in range(max_retries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=1):
                    break
        except (TimeoutError, ConnectionRefusedError):
            time.sleep(retry_delay)
    else:
        process.kill()
        rocklet_process.kill()
        pytest.fail("Server did not start within the expected time")

    logger.info(f"Admin server started on port {port}")
    yield RemoteServer(port)

    process.terminate()
    rocklet_process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        rocklet_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        rocklet_process.kill()


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
    cpus = getattr(request, "param", {}).get("cpus", 1.0)
    memory = getattr(request, "param", {}).get("memory", "4g")

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
