import time
from pathlib import Path

import pytest
import ray
from ray.util.state import list_actors

from rock.config import RockConfig
from rock.logger import init_logger

logger = init_logger(__name__)


@pytest.fixture(scope="session", autouse=True)
def rock_config():
    config_path = Path(__file__).parent.parent.parent / "rock-conf" / "rock-test.yml"
    return RockConfig.from_env(config_path=config_path)


@pytest.fixture(scope="session")
def ray_init_shutdown(rock_config: RockConfig):
    ray_config = rock_config.ray
    ray_namespace = ray_config.namespace

    # if not ray is initialized
    if not ray.is_initialized():
        ray.init(
            address=ray_config.address,
            namespace=ray_namespace,
            runtime_env=ray_config.runtime_env,
            resources=ray_config.resources,
        )
    yield

    try:
        actors = list_actors(filters=[("state", "=", "ALIVE"), ("namespace", "=", ray_namespace)])
        for actor in actors:
            try:
                actor_handle = ray.get_actor(actor["name"], namespace=ray_namespace)
                ray.kill(actor_handle)
                logger.warning(f"Killed actor: {actor['name']}")
            except Exception as e:
                logger.warning(f"Failed to kill actor {actor['name']}: {e}")

        # Wait for cleanup to complete
        time.sleep(2)
    except Exception as e:
        logger.warning(f"Failed to list or clean up actors: {e}")
    finally:
        try:
            ray.shutdown()
        except Exception as e:
            logger.warning(f"Failed to shutdown Ray: {e}")
