import asyncio
from pathlib import Path

import pytest

from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelService
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)

MODEL_PAYLOAD = (
    '{"id":"chat-","object":"chat.completion","created":1769156933,"model":"",'
    '"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"Hello! I am ROCK"}}]}'
)


async def model_service_loop(model_service: ModelService) -> None:
    """Main loop for Whale ModelService interaction (single fixed payload)."""
    if not model_service:
        raise Exception("ModelService is not initialized")

    index = 0
    total_calls = 0
    response_payload = None

    try:
        while True:
            agent_request_json_str = await model_service.anti_call_llm(
                index=index,
                response_payload=response_payload,
            )

            if agent_request_json_str == "SESSION_END":
                logger.info("ModelService session ended")
                break

            response_payload = MODEL_PAYLOAD

            total_calls += 1
            index += 1

        logger.info(f"ModelService loop completed (iterations: {index}, API calls: {total_calls})")

    except Exception as e:
        logger.error(
            f"ModelService loop failed (iteration: {index}, calls: {total_calls}): {str(e)}",
            exc_info=True,
        )
        raise


async def _run_agent_with_model_service(
    sandbox_instance: "Sandbox",
    monkeypatch,
    *,
    config_path: str,
    prompt: str = "Hello",
) -> str:
    test_dir = Path(__file__).resolve().parent
    monkeypatch.chdir(test_dir)

    await sandbox_instance.agent.install(config=config_path)

    agent_run_task = asyncio.create_task(sandbox_instance.agent.run(prompt))
    model_service_task = asyncio.create_task(model_service_loop(sandbox_instance.agent.model_service))

    agent_result, model_service_result = await asyncio.gather(
        agent_run_task, model_service_task, return_exceptions=True
    )

    if isinstance(agent_result, Exception):
        raise agent_result
    if isinstance(model_service_result, Exception):
        raise model_service_result

    return agent_result.output


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_rock_agent_run_iflow(sandbox_instance: Sandbox, monkeypatch) -> None:
    output = await _run_agent_with_model_service(sandbox_instance, monkeypatch, config_path="iflow_config.yaml")
    assert "Hello! I am ROCK" in output


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_rock_agent_run_langgraph(sandbox_instance: Sandbox, monkeypatch) -> None:
    output = await _run_agent_with_model_service(sandbox_instance, monkeypatch, config_path="langgraph_config.yaml")
    assert "Hello! I am ROCK" in output
