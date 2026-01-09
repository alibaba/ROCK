import asyncio
from pathlib import Path

import pytest

from rock import env_vars
from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.agent.swe_agent import DEFAULT_RUN_SINGLE_CONFIG, SweAgent, SweAgentConfig
from rock.sdk.sandbox.client import RunMode, Sandbox
from rock.sdk.sandbox.model_service.base import ModelServiceConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


async def model_service_loop(swe_agent: SweAgent, inference_gen) -> None:
    """Main loop for Whale ModelService interaction."""

    if not swe_agent.model_service:
        raise Exception("ModelService is not initialized in agent")

    index = 0
    response_payload = None
    total_calls = 0

    try:
        while True:
            # Get agent request from ModelService
            agent_request_json_str = await swe_agent.model_service.anti_call_llm(
                index=index,
                response_payload=response_payload,
            )

            # Check if session ended
            if agent_request_json_str == "SESSION_END":
                logger.info("ModelService session ended")
                break

            # Get next inference response from generator
            response_payload = await anext(inference_gen, None)
            if response_payload is None:
                logger.info("Inference file ended")
                break

            total_calls += 1
            index += 1

        logger.info(f"ModelService loop completed (iterations: {index}, API calls: {total_calls})")

    except Exception as e:
        logger.error(
            f"ModelService loop failed (iteration: {index}, calls: {total_calls}): {str(e)}",
            exc_info=True,
        )
        raise


async def call_model_inference_generator(filepath: str = "infer_data/qwen3_coder_plus.jsonl"):
    """Async generator that yields each line from the inference data file.

    Args:
        filepath: Relative path to the JSONL file containing inference data

    Yields:
        JSON string for each line in the file
    """
    current_dir = Path(__file__).parent
    full_path = current_dir / filepath

    with open(full_path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


def _get_python_install_cmd() -> str:
    """Get the Python installation command."""
    return env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD


async def _init_git_repository(sandbox: Sandbox, repo_path: str) -> None:
    """Initialize a git repository with an initial commit.

    Args:
        sandbox: Sandbox instance
        repo_path: Path to initialize git repository
    """
    commands = [
        f"mkdir -p {repo_path}",
        f"cd {repo_path} && git init",
        f"cd {repo_path} && git config user.email 'test@example.com'",
        f"cd {repo_path} && git config user.name 'Test User'",
        f"cd {repo_path} && touch 1.txt && echo 'Hello World' > 1.txt",
        f"cd {repo_path} && git add 1.txt",
        f"cd {repo_path} && git commit -m 'Initial commit'",
    ]

    for cmd in commands:
        result = await sandbox.execute(Command(command=["bash", "-c", cmd]))
        if result.exit_code != 0:
            logger.error(f"Git initialization failed: {cmd}")
            logger.error(f"Output: {result.stdout}")
            logger.error(f"Error: {result.stderr}")
            raise RuntimeError(f"Failed to execute: {cmd}")


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_swe_agent_run(sandbox_instance: Sandbox) -> None:
    """Test SWE-Agent installation with integrated model service."""

    python_install_cmd = _get_python_install_cmd()
    project_path = "/root/test_swe_agent"
    test_instance_id = "test_instance_id"
    try:
        # Initialize SWE-Agent configuration
        model_service_config = ModelServiceConfig(
            python_install_cmd=python_install_cmd,
        )

        run_single_config = DEFAULT_RUN_SINGLE_CONFIG.copy()
        run_single_config["agent"]["model"]["name"] = "openai/Qwen3-Coder-Plus"
        run_single_config["agent"]["model"]["api_base"] = "http://127.0.0.1:8080/v1/"
        run_single_config["agent"]["model"]["api_key"] = "just_a_test_key"

        swe_agent_config = SweAgentConfig(
            agent_type="swe-agent",
            version="unknown",
            default_run_single_config=run_single_config,
            model_service_config=model_service_config,
            python_install_cmd=python_install_cmd,
            # pre_startup_bash_cmd_list=[
            #     *env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST,
            #     "which pip > /dev/null 2>&1 && pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ || true",
            # ],
        )

        # Initialize and setup the agent
        sandbox_instance.agent = SweAgent(sandbox_instance, swe_agent_config)
        await sandbox_instance.agent.init()
        await sandbox_instance.agent.start_model_service()

        # Verify health check
        result = await sandbox_instance.execute(Command(command=["bash", "-c", "curl -s http://localhost:8080/health"]))
        assert result.exit_code == 0, f"Health check failed: {result.stdout}"
        assert "healthy" in result.stdout, f"Unexpected health response: {result.stdout}"

        # Initialize git repository
        await _init_git_repository(sandbox_instance, project_path)

        # Create inference generator
        inference_gen = call_model_inference_generator()

        # Run agent and model service in parallel
        agent_run_task = asyncio.create_task(
            sandbox_instance.agent.run(
                problem_statement="rename 1.txt to 2.txt",
                project_path=project_path,
                instance_id=test_instance_id,
                agent_run_timeout=1800,
                agent_run_check_interval=30,
            )
        )

        whale_service_task = asyncio.create_task(model_service_loop(sandbox_instance.agent, inference_gen))

        results = await asyncio.gather(agent_run_task, whale_service_task, return_exceptions=True)

        # Check for task failures
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Task {i} failed: {type(result).__name__}: {str(result)}")

        patch_path = (
            f"{swe_agent_config.swe_agent_workdir}/{test_instance_id}/{test_instance_id}/{test_instance_id}.patch"
        )

        file_content = await sandbox_instance.arun(cmd=f"cat {patch_path}", mode=RunMode.NOHUP)

        assert (
            file_content.output
            == "diff --git a/1.txt b/2.txt\r\nsimilarity index 100%\r\nrename from 1.txt\r\nrename to 2.txt"
        )

    except Exception as e:
        logger.error(f"Test failed: {type(e).__name__}: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Test cleanup completed")
