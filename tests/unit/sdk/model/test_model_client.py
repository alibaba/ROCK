import pytest

from rock.sdk.model.client import ModelClient


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
