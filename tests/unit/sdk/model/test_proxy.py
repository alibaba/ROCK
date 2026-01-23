from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response

from rock.sdk.model.server.api.proxy import perform_llm_request, proxy_router
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.main import lifespan

# Initialize a temporary FastAPI application for testing the router
test_app = FastAPI()
test_app.include_router(proxy_router)

mock_config = ModelServiceConfig()
test_app.state.model_service_config = mock_config

@pytest.mark.asyncio
async def test_chat_completions_routing_success():
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
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "hello"}]
            }
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        call_args = mock_request.call_args[0]
        assert call_args[0] == "https://api.openai.com/v1/chat/completions"
        assert mock_request.called


@pytest.mark.asyncio
async def test_chat_completions_fallback_to_default_when_not_found():
    """
    Test that an unrecognized model name correctly falls back to the 'default' URL.
    """
    patch_path = 'rock.sdk.model.server.api.proxy.perform_llm_request'

    with patch(patch_path, new_callable=AsyncMock) as mock_request:
        mock_resp = MagicMock(spec=Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "chat-fallback", "choices": []}
        mock_request.return_value = mock_resp

        config = test_app.state.model_service_config
        default_base_url = config.proxy_rules["default"].rstrip("/")
        expected_target_url = f"{default_base_url}/chat/completions"

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "model": "some-random-unsupported-model", # This model is NOT in proxy_rules
                "messages": [{"role": "user", "content": "hello"}]
            }
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200

        # Verify that perform_llm_request was called with the DEFAULT URL
        call_args = mock_request.call_args[0]
        actual_url = call_args[0]

        assert actual_url == expected_target_url
        assert mock_request.called


@pytest.mark.asyncio
async def test_chat_completions_routing_absolute_fail():
    """
    Test that both the specific model and the 'default' rule are missing.
    """
    empty_config = ModelServiceConfig()
    empty_config.proxy_rules = {}

    with patch.object(test_app.state, 'model_service_config', empty_config):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "model": "any-model",
                "messages": [{"role": "user", "content": "hello"}]
            }
            response = await ac.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not configured" in detail


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


@pytest.mark.asyncio
async def test_lifespan_initialization_with_config(tmp_path):
    """
    Test that the application correctly initializes and overrides defaults
    when a valid configuration file path is provided.
    """
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({
        "proxy_rules": {"my-model": "http://custom-url"},
        "request_timeout": 50
    }))

    # Initialize App and simulate CLI argument passing via app.state
    app = FastAPI(lifespan=lifespan)
    app.state.config_path = str(conf_file)

    async with lifespan(app):
        config = app.state.model_service_config
        # Verify that the config reflects file content instead of defaults
        assert config.proxy_rules["my-model"] == "http://custom-url"
        assert config.request_timeout == 50
        assert "gpt-3.5-turbo" not in config.proxy_rules


@pytest.mark.asyncio
async def test_lifespan_initialization_no_config():
    """
    Test that the application initializes with default ModelServiceConfig 
    settings when no configuration file path is provided.
    """
    app = FastAPI(lifespan=lifespan)
    app.state.config_path = None

    async with lifespan(app):
        config = app.state.model_service_config
        # Verify that default rules (e.g., 'gpt-3.5-turbo') are loaded
        assert "gpt-3.5-turbo" in config.proxy_rules
        assert config.request_timeout == 120


@pytest.mark.asyncio
async def test_lifespan_invalid_config_path():
    """
    Test that providing a non-existent configuration file path causes the
    lifespan to raise a FileNotFoundError, ensuring fail-fast behavior.
    """
    app = FastAPI(lifespan=lifespan)
    app.state.config_path = "/tmp/non_existent_file.yml"

    # Expect FileNotFoundError to be raised during startup
    with pytest.raises(FileNotFoundError):
        async with lifespan(app):
            pass
