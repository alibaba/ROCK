import asyncio
import inspect
import tempfile
from pathlib import Path

import pytest

from rock import env_vars
from rock.sdk.model.client import ModelClient
from rock.sdk.model.server.config import REQUEST_END_MARKER, REQUEST_START_MARKER


@pytest.mark.asyncio
async def test_parse_request_line():
    client = ModelClient()
    content = 'LLM_REQUEST_START{"model": "gpt-3.5-turbo", "messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Hello! How are you?"}], "temperature": 0.7, "stream": false}LLM_REQUEST_END{"timestamp": 1764147605564, "index": 1}'
    request_json, meta = await client.parse_request_line(content)
    assert 1 == meta.get("index")
    assert "gpt-3.5-turbo" in request_json

    content = "SESSION_END"
    request_json, meta = await client.parse_request_line(content)
    assert content == request_json


@pytest.mark.asyncio
async def test_parse_response_line():
    client = ModelClient()
    content = 'LLM_RESPONSE_START{"content": "mock content", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1}}LLM_RESPONSE_END{"timestamp": 1764160122979, "index": 1}'
    response_json, meta = await client.parse_response_line(content)
    assert 1 == meta.get("index")
    assert "mock content" in response_json


# ==================== Timeout Tests ====================


@pytest.mark.asyncio
async def test_pop_request_raises_timeout_error_when_timeout_expires():
    """Test that pop_request raises TimeoutError when timeout expires."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        # Write a request with index 1, but we'll ask for index 2
        f.write(f'{REQUEST_START_MARKER}{{"model": "gpt-4"}}{REQUEST_END_MARKER}{{"index": 1}}\n')
        log_file = f.name

    try:
        client = ModelClient(log_file_name=log_file)
        # Should timeout because index 2 doesn't exist
        with pytest.raises(TimeoutError, match="pop_request timed out"):
            await client.pop_request(index=2, timeout=0.5)
    finally:
        Path(log_file).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_wait_for_first_request_raises_timeout_error_when_timeout_expires():
    """Test that wait_for_first_request raises TimeoutError when timeout expires."""
    # Use a non-existent file
    client = ModelClient(log_file_name="/non/existent/path/file.log")
    with pytest.raises(TimeoutError, match="wait_for_first_request timed out"):
        await client.wait_for_first_request(timeout=0.5)


# ==================== Function Signature Tests ====================


def test_pop_request_timeout_default_is_from_env_vars():
    """Test that pop_request timeout parameter default is env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT."""
    sig = inspect.signature(ModelClient.pop_request)
    timeout_param = sig.parameters["timeout"]
    # The default value should equal env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT (evaluated at import time)
    assert timeout_param.default == env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT


def test_wait_for_first_request_timeout_default_is_from_env_vars():
    """Test that wait_for_first_request timeout parameter default is env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT."""
    sig = inspect.signature(ModelClient.wait_for_first_request)
    timeout_param = sig.parameters["timeout"]
    # The default value should equal env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT (evaluated at import time)
    assert timeout_param.default == env_vars.ROCK_MODEL_CLIENT_POLL_TIMEOUT


# ==================== Cancellation Tests ====================


@pytest.mark.asyncio
async def test_pop_request_propagates_cancelled_error():
    """Test that pop_request properly propagates asyncio.CancelledError."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        # Write a request with index 1, but we'll ask for index 2
        f.write(f'{REQUEST_START_MARKER}{{"model": "gpt-4"}}{REQUEST_END_MARKER}{{"index": 1}}\n')
        log_file = f.name

    try:
        client = ModelClient(log_file_name=log_file)

        async def cancel_after_delay():
            await asyncio.sleep(0.3)
            raise asyncio.CancelledError()

        task = asyncio.create_task(client.pop_request(index=2, timeout=10.0))
        # Cancel the task after a short delay
        await asyncio.sleep(0.3)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        Path(log_file).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_wait_for_first_request_propagates_cancelled_error():
    """Test that wait_for_first_request properly propagates asyncio.CancelledError."""
    client = ModelClient(log_file_name="/non/existent/path/file.log")

    task = asyncio.create_task(client.wait_for_first_request(timeout=10.0))
    # Cancel the task after a short delay
    await asyncio.sleep(0.3)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
