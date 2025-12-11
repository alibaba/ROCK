import asyncio
import json
import logging
import os
import subprocess
import threading

import httpx
import pytest

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_model_service():
    os.environ["ROCK_MODEL_SERVICE_DATA_DIR"] = "/tmp/rock_model_service_test_data"

    subprocess.run(["rock", "model-service", "start"])

    mock_agent_process = subprocess.Popen(["sleep", "200"])
    agent_pid = mock_agent_process.pid

    subprocess.run(["rock", "model-service", "watch-agent", "--pid", str(agent_pid)])

    try:
        task = asyncio.create_task(mock_agent_call(agent_pid))
        thread = threading.Thread(target=mock_roll_call)
        thread.start()
        await task
        thread.join()
    finally:
        subprocess.run(["rock", "model-service", "stop"])


async def mock_agent_call(agent_pid: int):
    await make_request()
    await make_request()
    await make_request()
    logger.info(f"mock agent process exiting, start to kill agent process {agent_pid}")
    subprocess.run(["kill", "-9", str(agent_pid)])


def mock_roll_call():
    result = subprocess.run(["rock", "model-service", "anti-call-llm", "--index=0"], capture_output=True, text=True)
    print(f"result of anti-call-llm is: {result.stdout}")
    assert "You are a helpful assistant." in result.stdout

    result = subprocess.run(
        [
            "rock",
            "model-service",
            "anti-call-llm",
            "--index=1",
            "--response",
            json.dumps(mock_response(), ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
    )
    print(f"result of anti-call-llm is: {result.stdout}")
    assert "You are a helpful assistant." in result.stdout

    # anti-call-llm --index=2 for twice: mock client retry
    result = subprocess.run(
        [
            "rock",
            "model-service",
            "anti-call-llm",
            "--index=2",
            "--response",
            json.dumps(mock_response(), ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
    )
    print(f"result of anti-call-llm is: {result.stdout}")
    assert "You are a helpful assistant." in result.stdout

    result = subprocess.run(
        [
            "rock",
            "model-service",
            "anti-call-llm",
            "--index=2",
            "--response",
            json.dumps(mock_response(), ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
    )
    print(f"result of anti-call-llm is: {result.stdout}")
    assert "You are a helpful assistant." in result.stdout

    result = subprocess.run(
        [
            "rock",
            "model-service",
            "anti-call-llm",
            "--index=3",
            "--response",
            json.dumps(mock_response(), ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
    )
    print(f"result of anti-call-llm is: {result.stdout}")
    assert "SESSION_END\n" == result.stdout


async def make_request():
    url = "http://127.0.0.1:8080/v1/chat/completions"
    payload = await mock_request()
    logger.info("start to call llm")
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        assert "mock content" in response.text
        return response


async def mock_request() -> dict:
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello! How are you?"},
        ],
        "temperature": 0.7,
        "stream": False,
    }
    return payload


def mock_response() -> dict:
    result = {
        "content": "mock content",
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 1,
        },
    }
    return result


if "__main__" == __name__:
    asyncio.run(test_model_service())
