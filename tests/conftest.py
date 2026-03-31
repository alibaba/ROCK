import logging
import os
import uuid
from pathlib import Path

import pytest

from rock import env_vars
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
