import logging
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from rock import env_vars
from rock.envhub.core.envhub import DockerEnvHub


@pytest.fixture(autouse=True, scope="session")
def configure_logging():
    """Automatically configure logging for all tests"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(filename)s:%(lineno)d -- %(message)s",
        force=True,  # Force reconfiguration
    )
    log_dir = env_vars.ROCK_LOGGING_PATH
    if not Path(log_dir).is_absolute():
        # Relative to project root directory
        project_root = Path(__file__).parent.parent  # Project root directory
        log_dir = str(project_root / log_dir)
        env_vars.ROCK_LOGGING_PATH = log_dir


@pytest.fixture(scope="function")
def docker_env_hub():
    """Create a DockerEnvHub instance with a temporary database"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        db_path = tmp_file.name

    db_url = f"sqlite:///{db_path}"
    hub = DockerEnvHub(db_url=db_url)

    yield hub

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture(scope="function")
def docker_available():
    """Check if docker is available."""
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True, timeout=5)
        subprocess.run(["docker", "images"], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Docker not available")


@pytest.fixture(scope="funtion")
def default_image_available():
    """Check if default docker image is available."""
    try:
        result = subprocess.run(
            [
                "docker",
                "images",
                "-q",
                env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if not result.stdout.strip():
            pytest.skip("envhub default docker image not found")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Cannot check docker images")
