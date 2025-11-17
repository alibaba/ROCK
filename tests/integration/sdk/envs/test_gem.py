import socket
import subprocess
import time

import gem
import pytest

import rock
from rock import env_vars
from rock.sdk.envs import RockEnv
from rock.utils.concurrent_helper import run_until_complete
from rock.utils.docker import DockerUtil
from rock.utils.system import find_free_port
from tests.integration.conftest import RemoteServer


@pytest.fixture(scope="session")
def admin_remote_server():
    port = run_until_complete(find_free_port())

    process = subprocess.Popen(
        [
            "admin",
            "--env",
            "local",
            "--role",
            "admin",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the server to start
    max_retries = 10
    retry_delay = 3
    for _ in range(max_retries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except (TimeoutError, ConnectionRefusedError):
            time.sleep(retry_delay)
    else:
        process.kill()
        pytest.fail("Server did not start within the expected time")

    yield RemoteServer(port)

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(
    not (DockerUtil.is_docker_available() and DockerUtil.is_image_available(env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE)),
    reason=f"Requires Docker and image {env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE}",
)
def test_rock_env(admin_remote_server: RemoteServer, monkeypatch):
    # For now, don't use the sandbox_server fixture approach, manually start admin for testing
    # Create environment
    monkeypatch.setattr(env_vars, "ROCK_BASE_URL", f"http://127.0.0.1:{admin_remote_server.port}")

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
