import os

import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.agent.swe_agent import SweAgent, SweAgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelServiceConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


async def _verify_exists(sandbox: Sandbox, directory_path: str, items: set[str]) -> None:
    """Verify that expected items exist in the directory."""
    result = await sandbox.execute(Command(command="ls", cwd=directory_path))
    assert result.exit_code == 0, f"Failed to list {directory_path}"

    for item in items:
        assert item in result.stdout, f"'{item}' not found in {directory_path}"

    logger.info(f"Directory {directory_path} contents: {result.stdout}")


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_swe_agent_initialization(sandbox_instance: Sandbox):
    """Test SWE-Agent installation and initialization."""

    # 1. Initialize SWE-Agent
    swe_agent_config = SweAgentConfig()
    sandbox_instance.agent = SweAgent(sandbox_instance, swe_agent_config)

    # 2. Initialize the agent
    await sandbox_instance.agent.init()

    # 3. Verify agent directory exists in root
    agent_dir_name = os.path.basename(swe_agent_config.agent_installed_dir)
    await _verify_exists(sandbox_instance, "/", {agent_dir_name})

    # 4. Verify agent installation directories
    await _verify_exists(sandbox_instance, swe_agent_config.agent_installed_dir, {"SWE-agent"})

    # 5. Verify Python executables
    python_bin_path = sandbox_instance.agent.rt_env.bin_dir
    await _verify_exists(sandbox_instance, python_bin_path, {"sweagent"})


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_swe_agent_with_model_service(sandbox_instance: Sandbox):
    """Test SWE-Agent installation with integrated model service."""

    # 1. Initialize SWE-Agent with model service
    model_service_config = ModelServiceConfig()
    swe_agent_config = SweAgentConfig(model_service_config=model_service_config)
    sandbox_instance.agent = SweAgent(sandbox_instance, swe_agent_config)

    # 2. Initialize the agent
    await sandbox_instance.agent.init()

    # 3. Verify Python executables
    python_bin_path = sandbox_instance.model_service.rt_env.bin_dir
    await _verify_exists(sandbox_instance, python_bin_path, {"rock"})
