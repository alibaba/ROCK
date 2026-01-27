import pytest

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.runtime_env import NodeRuntimeEnvConfig, PythonRuntimeEnvConfig
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from tests.integration.conftest import SKIP_IF_NO_DOCKER


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_python_runtime_env(sandbox_instance: Sandbox):
    """Test Python runtime env basic initialization."""
    config = PythonRuntimeEnvConfig(version="3.11")
    python_env = await RuntimeEnv.create(sandbox_instance, config)

    result = await python_env.run("python --version")
    assert "3.11" in result.output


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_node_runtime_env(sandbox_instance: Sandbox):
    """Test Node runtime env basic initialization."""
    config = NodeRuntimeEnvConfig(version="22.18.0")
    node_env = await RuntimeEnv.create(sandbox_instance, config)

    result = await node_env.run("node --version")
    assert "22.18.0" in result.output
