import asyncio
from pathlib import Path

import pytest

from rock.logger import init_logger
from rock.sdk.sandbox.agent.rock_agent import RockAgent
from rock.sdk.sandbox.client import Sandbox
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


async def model_service_loop(agent: RockAgent, inference_gen) -> None:
    """Main loop for Whale ModelService interaction."""

    if not agent.model_service:
        raise Exception("ModelService is not initialized in agent")

    index = 0
    response_payload = None
    total_calls = 0

    try:
        while True:
            # Get agent request from ModelService
            agent_request_json_str = await agent.model_service.anti_call_llm(
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


async def call_model_inference_generator():
    line = '{"id":"chat-","object":"chat.completion","created":1769156933,"model":"","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"Hello! I am ROCK"}}]}'

    yield line


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_rock_agent_run_iflow(
    sandbox_instance: Sandbox,
) -> None:
    config_path = Path(__file__).resolve().parent / "rock_agent_config.yaml"
    await sandbox_instance.agent.install(config_path=str(config_path))
    agent_run_task = asyncio.create_task(sandbox_instance.agent.run("Hello"))
    model_service_task = asyncio.create_task(
        model_service_loop(sandbox_instance.agent, call_model_inference_generator())
    )

    results = await asyncio.gather(agent_run_task, model_service_task, return_exceptions=True)

    agent_result = results[0]
    model_service_result = results[1]

    if isinstance(agent_result, Exception):
        raise agent_result
    if isinstance(model_service_result, Exception):
        raise model_service_result

    agent_output = agent_result.output

    assert "Hello! I am ROCK" in agent_output
