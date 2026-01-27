import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.agent.swe_agent import SweAgent, SweAgentConfig
from rock.sdk.sandbox.client import Sandbox
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
    swe_agent_config = SweAgentConfig(
        agent_type="swe-agent",
        version="unknown",
        project_path="/root",
    )
    sandbox_instance.agent = SweAgent(sandbox_instance)

    # 2. Initialize the agent
    await sandbox_instance.agent.install(swe_agent_config)

    await _verify_exists(sandbox_instance, swe_agent_config.agent_installed_dir, {"SWE-agent"})

    await _verify_exists(sandbox_instance, sandbox_instance.agent.runtime_env.bin_dir, {"sweagent"})
