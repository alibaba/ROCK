from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response

from rock.sdk.model.server.api.proxy import perform_llm_request, proxy_router
from rock.sdk.model.server.config import ModelProxyConfig

# Initialize a temporary FastAPI application for testing the router
test_app = FastAPI()
test_app.include_router(proxy_router)

mock_config = ModelProxyConfig(
    proxy_rules={
        "qwen": "http://whale.url",
        "default": "http://default.url"
    },
    retryable_status_codes=[429, 499],
    request_timeout=60
)
test_app.state.model_proxy_config = mock_config

@pytest.mark.asyncio
async def test_chat_completions_routing():
    """
    Test the high-level routing logic.
    """
    patch_path = 'rock.sdk.model.server.api.proxy.perform_llm_request'

    with patch(patch_path, new_callable=AsyncMock) as mock_request:
        mock_resp = MagicMock(spec=Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "chat-123", "choices": []}
        mock_request.return_value = mock_resp

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "model": "Qwen2.5-72B", 
                "messages": [{"role": "user", "content": "hello"}]
            }
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        call_args = mock_request.call_args[0]
        assert call_args[0] == "http://whale.url" 
        assert mock_request.called


@pytest.mark.asyncio
async def test_perform_llm_request_retry_on_whitelist():
    """
    Test that the proxy retries when receiving a whitelisted error code.
    """
    client_post_path = 'rock.sdk.model.server.api.proxy.http_client.post'

    # Patch asyncio.sleep inside the retry module to avoid actual waiting
    with patch(client_post_path, new_callable=AsyncMock) as mock_post, \
         patch('rock.utils.retry.asyncio.sleep', return_value=None):

        # 1. Setup Failed Response (429)
        resp_429 = MagicMock(spec=Response)
        resp_429.status_code = 429
        error_429 = HTTPStatusError(
            "Rate Limited", 
            request=MagicMock(spec=Request), 
            response=resp_429
        )

        # 2. Setup Success Response (200)
        resp_200 = MagicMock(spec=Response)
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}

        # Sequence: Fail with 429, then Succeed with 200
        mock_post.side_effect = [error_429, resp_200]

        result = await perform_llm_request("http://fake.url", {}, {}, mock_config)

        assert result.status_code == 200
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_perform_llm_request_no_retry_on_non_whitelist():
    """
    Test that the proxy DOES NOT retry for non-retryable codes (e.g., 401).
    It should return the error response immediately.
    """
    client_post_path = 'rock.sdk.model.server.api.proxy.http_client.post'

    with patch(client_post_path, new_callable=AsyncMock) as mock_post:
        # Mock 401 Unauthorized (NOT in the retry whitelist)
        resp_401 = MagicMock(spec=Response)
        resp_401.status_code = 401
        resp_401.json.return_value = {"error": "Invalid API Key"}

        # The function should return this response directly
        mock_post.return_value = resp_401

        result = await perform_llm_request("http://fake.url", {}, {}, mock_config)

        assert result.status_code == 401
        # Call count must be 1, meaning no retries were attempted
        assert mock_post.call_count == 1 


@pytest.mark.asyncio
async def test_perform_llm_request_network_timeout_retry():
    """
    Test that network-level exceptions (like Timeout) also trigger retries.
    """
    client_post_path = 'rock.sdk.model.server.api.proxy.http_client.post'

    with patch(client_post_path, new_callable=AsyncMock) as mock_post, \
         patch('rock.utils.retry.asyncio.sleep', return_value=None):

        resp_200 = MagicMock(spec=Response)
        resp_200.status_code = 200

        mock_post.side_effect = [httpx.TimeoutException("Network Timeout"), resp_200]

        result = await perform_llm_request("http://fake.url", {}, {}, mock_config)

        assert result.status_code == 200
        assert mock_post.call_count == 2
