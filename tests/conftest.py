import logging
import uuid
from pathlib import Path

import pytest

from rock import env_vars


@pytest.fixture(autouse=True, scope="session")
def configure_logging():
    """Automatically configure logging for all tests"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(filename)s:%(lineno)d -- %(message)s",
        force=True,  # Force reconfiguration
    )
    log_dir = env_vars.ROCK_LOGGING_PATH
    if log_dir and not Path(log_dir).is_absolute():
        # Relative to project root directory
        project_root = Path(__file__).parent.parent  # Project root directory
        log_dir = str(project_root / log_dir)
        env_vars.ROCK_LOGGING_PATH = log_dir

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@pytest.fixture(name="container_name")
def random_container_name() -> str:
    container_name = uuid.uuid4().hex
    return container_name


from rock.utils.docker import DockerUtil


def pytest_collection_modifyitems(config, items):
    if not DockerUtil.is_docker_available():
        skip_docker = pytest.mark.skip(reason="Docker is not available")
        for item in items:
            if "need_docker" in item.keywords:
                item.add_marker(skip_docker)
