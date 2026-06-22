import asyncio
import json
import logging
import os
import subprocess
import tempfile
import threading

import httpx
import pytest

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_model_service():
    model_env = os.environ.copy()
    model_env["ROCK_LOGGING_FILE_NAME"] = "model_service.log"
    subprocess.run(["rock", "model-service", "start"], env=model_env)

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


@pytest.mark.asyncio
async def test_model_service_large_payload_via_response_file():
    """Verify --response-file works for payloads that would exceed ARG_MAX via --response."""
    model_env = os.environ.copy()
    model_env["ROCK_LOGGING_FILE_NAME"] = "model_service_large.log"
    subprocess.run(["rock", "model-service", "start"], env=model_env)

    mock_agent_process = subprocess.Popen(["sleep", "200"])
    agent_pid = mock_agent_process.pid

    subprocess.run(["rock", "model-service", "watch-agent", "--pid", str(agent_pid)])

    try:
        task = asyncio.create_task(mock_agent_call_single(agent_pid))
        thread = threading.Thread(target=mock_roll_call_large_payload)
        thread.start()
        await task
        thread.join()
    finally:
        subprocess.run(["rock", "model-service", "stop"])


async def mock_agent_call_single(agent_pid: int):
    await make_request()
    logger.info(f"mock agent process exiting, killing pid {agent_pid}")
    subprocess.run(["kill", "-9", str(agent_pid)])


def mock_roll_call_large_payload():
    # index=0: get first request (no response needed)
    result = subprocess.run(["rock", "model-service", "anti-call-llm", "--index=0"], capture_output=True, text=True)
    print(f"index=0 result: {result.stdout[:100]}")
    assert "You are a helpful assistant." in result.stdout

    # index=1: first prove --response with large payload hits ARG_MAX
    large_content = "x" * (500 * 1024)  # 500KB
    large_response = json.dumps({
        "id": "chatcmpl-large",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "mock content",
                "tool_calls": [{
                    "id": "call_large",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps({"path": "/tmp/large.py", "content": large_content}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }, ensure_ascii=False)
    print(f"Large payload size: {len(large_response)} bytes")

    print(f"[OLD] Trying --response with {len(large_response)} bytes as command-line arg...")
    try:
        result = subprocess.run(
            ["rock", "model-service", "anti-call-llm", "--index=1", "--response", large_response],
            capture_output=True,
            text=True,
        )
        old_way_failed = result.returncode != 0
    except OSError as e:
        print(f"[OLD] FAILED as expected: {e}")
        old_way_failed = True
    assert old_way_failed, "--response with 500KB payload should fail with Argument list too long"

    print(f"[NEW] Trying --response-file with same {len(large_response)} bytes via file...")
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(large_response)

    result = subprocess.run(
        ["rock", "model-service", "anti-call-llm", "--index=1", "--response-file", tmp_path],
        capture_output=True,
        text=True,
    )
    print(f"[NEW] SUCCESS: {result.stdout.strip()}")
    assert result.returncode == 0, f"--response-file should succeed, stderr: {result.stderr[:200]}"
    assert not os.path.exists(tmp_path), f"Response file {tmp_path} should be deleted after read"
    assert "SESSION_END\n" == result.stdout


if "__main__" == __name__:
    asyncio.run(test_model_service())
