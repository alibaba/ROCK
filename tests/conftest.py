import logging
import os
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from rock import env_vars
from rock.utils import find_free_port, run_until_complete
from rock.utils.docker import DockerUtil

# Set test data directories at import time (before test collection triggers
# module-level constants in production code like TRAJ_FILE in config.py).
_project_root = Path(__file__).parent.parent
_workdir = _project_root / ".tmp" / "test_data"
_workdir.mkdir(parents=True, exist_ok=True)

_dirs = {
    "ROCK_MODEL_SERVICE_DATA_DIR": str(_workdir / "model"),
    "ROCK_SCHEDULER_STATUS_DIR": str(_workdir / "scheduler"),
    "ROCK_LOGGING_PATH": str(_workdir / "logs"),
    "ROCK_SERVICE_STATUS_DIR": str(_workdir / "status"),
}

for _key, _value in _dirs.items():
    setattr(env_vars, _key, _value)
    os.environ[_key] = _value


@pytest.fixture(autouse=True, scope="session")
def test_workdir():
    """Unified test data directory under project_root/.tmp/test_data.

    Redirects all ROCK directory env vars to avoid PermissionError
    on non-container environments (e.g. /data/logs -> .tmp/test_data/model).

    The actual env var setup happens at module level above (before collection),
    this fixture exposes the workdir path for tests that need it.
    """
    yield _workdir


@pytest.fixture(autouse=True, scope="session")
def configure_logging(test_workdir):
    """Automatically configure logging for all tests.

    Depends on test_workdir to ensure ROCK_LOGGING_PATH is set first.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(filename)s:%(lineno)d -- %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@pytest.fixture(name="container_name")
def random_container_name() -> str:
    container_name = uuid.uuid4().hex
    return container_name


def pytest_collection_modifyitems(config, items):
    if not DockerUtil.is_docker_available():
        skip_docker = pytest.mark.skip(reason="Docker is not available")
        for item in items:
            if "need_docker" in item.keywords:
                item.add_marker(skip_docker)


@contextmanager
def start_rocklet_process():
    """Start a local ``rocklet`` process on a free port for tests.

    Exports ``ROCK_WORKER_ROCKLET_PORT`` (read at call time by
    ``env_vars.ROCK_WORKER_ROCKLET_PORT``) so sandbox status probing
    (``get_remote_status``) reaches a live rocklet instead of failing with
    ConnectError. Yields the port; terminates the process and restores the
    original env var on exit.
    """
    port = run_until_complete(find_free_port())
    original_port = os.environ.get("ROCK_WORKER_ROCKLET_PORT")
    os.environ["ROCK_WORKER_ROCKLET_PORT"] = str(port)
    process = subprocess.Popen(["rocklet", "--port", str(port)], stdout=None, stderr=None)
    try:
        for _ in range(10):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except (TimeoutError, ConnectionRefusedError):
                time.sleep(3)
        else:
            process.kill()
            pytest.fail("Rocklet did not start within the expected time")
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if original_port is None:
            os.environ.pop("ROCK_WORKER_ROCKLET_PORT", None)
        else:
            os.environ["ROCK_WORKER_ROCKLET_PORT"] = original_port
