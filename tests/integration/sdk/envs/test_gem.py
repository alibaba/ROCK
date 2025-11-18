import gem
import pytest

import rock
from rock import env_vars
from rock.sdk.envs import RockEnv
from tests.integration.conftest import SKIP_IF_NO_DOCKER, RemoteServer


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
def test_rock_env(admin_remote_server: RemoteServer, monkeypatch):
    # For now, don't use the sandbox_server fixture approach, manually start admin for testing
    # Create environment
    monkeypatch.setattr(env_vars, "ROCK_BASE_URL", f"{admin_remote_server.endpoint}:{admin_remote_server.port}")

    env_id = "game:Sokoban-v0-easy"
    example_gem_env: gem.Env = gem.make(env_id)
    env: RockEnv = rock.make(env_id)
    env.reset(seed=42)

    for _ in range(10):
        action = example_gem_env.sample_random_action()
        observation, reward, terminated, truncated, info = env.step(action)

        if terminated or truncated:
            break

    env.close()
