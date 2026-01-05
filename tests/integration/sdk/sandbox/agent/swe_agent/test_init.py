import os

import pytest

from rock import env_vars
from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.agent.swe_agent import SweAgent, SweAgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelServiceConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


def _get_python_install_cmd() -> str:
    """Get the Python installation command."""
    return env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD


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
    swe_agent_config = SweAgentConfig(
        agent_type="swe-agent",
        version="unknown",
        python_install_cmd=_get_python_install_cmd(),
    )
    sandbox_instance.agent = SweAgent(sandbox_instance, swe_agent_config)

    # 2. Initialize the agent
    await sandbox_instance.agent.init()

    # 3. Verify agent directory exists in root
    agent_dir_name = os.path.basename(swe_agent_config.swe_agent_workdir)
    await _verify_exists(sandbox_instance, "/", {agent_dir_name})

    # 4. Verify agent installation directories
    await _verify_exists(sandbox_instance, swe_agent_config.swe_agent_workdir, {"python", "SWE-agent"})

    # 5. Verify Python executables
    python_bin_path = f"{swe_agent_config.swe_agent_workdir}/python/bin"
    await _verify_exists(sandbox_instance, python_bin_path, {"sweagent"})


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_swe_agent_with_model_service(sandbox_instance: Sandbox):
    """Test SWE-Agent installation with integrated model service."""

    python_install_cmd = _get_python_install_cmd()

    # 1. Initialize SWE-Agent with model service
    model_service_config = ModelServiceConfig(python_install_cmd=python_install_cmd)
    swe_agent_config = SweAgentConfig(
        agent_type="swe-agent",
        version="unknown",
        python_install_cmd=python_install_cmd,
        model_service_config=model_service_config,
    )
    sandbox_instance.agent = SweAgent(sandbox_instance, swe_agent_config)

    # 2. Initialize the agent
    await sandbox_instance.agent.init()

    # 3. Verify both agent and model service directories exist in root
    agent_dir_name = os.path.basename(swe_agent_config.swe_agent_workdir)
    model_service_dir_name = os.path.basename(model_service_config.workdir)
    await _verify_exists(sandbox_instance, "/", {agent_dir_name, model_service_dir_name})

    # 4. Verify agent installation directories
    await _verify_exists(sandbox_instance, swe_agent_config.swe_agent_workdir, {"python", "SWE-agent"})

    # 5. Verify Python executables
    python_bin_path = f"{swe_agent_config.swe_agent_workdir}/python/bin"
    await _verify_exists(sandbox_instance, python_bin_path, {"sweagent"})
