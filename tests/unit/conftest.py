import logging
from pathlib import Path

import pytest

from rock import env_vars
from rock.config import RockConfig
from rock.utils.providers import RedisProvider


@pytest.fixture(scope="session")
def rock_config():
    return RockConfig.from_env()


@pytest.fixture(autouse=True)
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


@pytest.fixture
async def redis_provider(rock_config: RockConfig):
    redis_provider = RedisProvider(
        host=rock_config.redis.host,
        port=rock_config.redis.port,
        password=rock_config.redis.password,
    )
    await redis_provider.init_pool()
    return redis_provider
