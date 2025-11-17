import pytest

from rock.config import RockConfig


@pytest.fixture(scope="function")
def rock_config():
    return RockConfig.from_env()
