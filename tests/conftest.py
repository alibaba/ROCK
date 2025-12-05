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


@pytest.fixture(name="container_name")
def random_container_name() -> str:
    container_name = uuid.uuid4().hex
    return container_name


@pytest.fixture(scope="session", autouse=True)
def suppress_third_party_logs():
    """Suppress noisy third-party library logs during testing"""
    third_party_loggers = [
        "apscheduler",
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
    ]

    for logger_name in third_party_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
