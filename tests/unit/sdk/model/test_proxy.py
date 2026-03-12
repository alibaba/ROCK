from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response

from rock.sdk.model.server.api.proxy import perform_llm_request, proxy_router
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.main import create_config_from_args, lifespan
from rock.sdk.model.server.utils import (
    MODEL_SERVICE_REQUEST_COUNT,
    MODEL_SERVICE_REQUEST_RT,
    _get_or_create_metrics_monitor,
    record_traj,
)

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
    patch_path = "rock.sdk.model.server.api.proxy.perform_llm_request"

    with patch(patch_path, new_callable=AsyncMock) as mock_request:
        mock_resp = MagicMock(spec=Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "chat-123", "choices": []}
        mock_request.return_value = mock_resp

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]}
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
    patch_path = "rock.sdk.model.server.api.proxy.perform_llm_request"

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
                "model": "some-random-unsupported-model",  # This model is NOT in proxy_rules
                "messages": [{"role": "user", "content": "hello"}],
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

    with patch.object(test_app.state, "model_service_config", empty_config):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "any-model", "messages": [{"role": "user", "content": "hello"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not configured" in detail


@pytest.mark.asyncio
async def test_perform_llm_request_retry_on_whitelist():
    """
    Test that the proxy retries when receiving a whitelisted error code.
    """
    client_post_path = "rock.sdk.model.server.api.proxy.http_client.post"

    # Patch asyncio.sleep inside the retry module to avoid actual waiting
    with patch(client_post_path, new_callable=AsyncMock) as mock_post, patch(
        "rock.utils.retry.asyncio.sleep", return_value=None
    ):
        # 1. Setup Failed Response (429)
        resp_429 = MagicMock(spec=Response)
        resp_429.status_code = 429
        error_429 = HTTPStatusError("Rate Limited", request=MagicMock(spec=Request), response=resp_429)

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
    client_post_path = "rock.sdk.model.server.api.proxy.http_client.post"

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
    client_post_path = "rock.sdk.model.server.api.proxy.http_client.post"

    with patch(client_post_path, new_callable=AsyncMock) as mock_post, patch(
        "rock.utils.retry.asyncio.sleep", return_value=None
    ):
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
    conf_file.write_text(yaml.dump({"proxy_rules": {"my-model": "http://custom-url"}, "request_timeout": 50}))

    # Initialize App and load config from file
    config = ModelServiceConfig.from_file(str(conf_file))
    app = FastAPI(lifespan=lambda app: lifespan(app, config))

    async with lifespan(app, config):
        app_config = app.state.model_service_config
        # Verify that the config reflects file content instead of defaults
        assert app_config.proxy_rules["my-model"] == "http://custom-url"
        assert app_config.request_timeout == 50
        assert "gpt-3.5-turbo" not in app_config.proxy_rules


@pytest.mark.asyncio
async def test_lifespan_initialization_no_config():
    """
    Test that the application initializes with default ModelServiceConfig
    settings when no configuration file path is provided.
    """
    config = ModelServiceConfig()
    app = FastAPI(lifespan=lambda app: lifespan(app, config))

    async with lifespan(app, config):
        app_config = app.state.model_service_config
        # Verify that default rules (e.g., 'gpt-3.5-turbo') are loaded
        assert "gpt-3.5-turbo" in app_config.proxy_rules
        assert app_config.request_timeout == 120


@pytest.mark.asyncio
async def test_lifespan_invalid_config_path():
    """
    Test that providing a non-existent configuration file path causes
    ModelServiceConfig.from_file to raise a FileNotFoundError.
    """
    # Expect FileNotFoundError when loading from non-existent file
    with pytest.raises(FileNotFoundError):
        ModelServiceConfig.from_file("/tmp/non_existent_file.yml")


@pytest.mark.asyncio
async def test_proxy_base_url_overrides_proxy_rules(tmp_path):
    """
    Test that when proxy_base_url is set, all requests are forwarded to that URL,
    bypassing proxy_rules entirely.
    """
    config = ModelServiceConfig()
    config.proxy_base_url = "https://custom-endpoint.example.com/v1"

    test_app = FastAPI()
    test_app.state.model_service_config = config
    test_app.include_router(proxy_router)

    with patch("rock.sdk.model.server.api.proxy.perform_llm_request", new_callable=AsyncMock) as mock_request:
        mock_resp = MagicMock(spec=Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "chat-123", "choices": []}
        mock_request.return_value = mock_resp

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Even when requesting gpt-3.5-turbo, should forward to proxy_base_url
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        # Verify request was sent to proxy_base_url
        call_args = mock_request.call_args[0]
        assert call_args[0] == "https://custom-endpoint.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_config_loads_host_and_port_from_file(tmp_path):
    """
    Test that ModelServiceConfig correctly loads host and port from config file.
    """
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump({"host": "127.0.0.1", "port": 9000, "proxy_rules": {"my-model": "http://my-backend"}})
    )

    config = ModelServiceConfig.from_file(str(conf_file))

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.proxy_rules["my-model"] == "http://my-backend"


def test_config_default_host_and_port():
    """
    Test default values for host and port.
    """
    config = ModelServiceConfig()

    assert config.host == "0.0.0.0"
    assert config.port == 8080


@pytest.mark.asyncio
async def test_config_loads_retryable_status_codes_from_file(tmp_path):
    """
    Test that ModelServiceConfig correctly loads retryable_status_codes from config file.
    """
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"retryable_status_codes": [429, 500, 502, 503]}))

    config = ModelServiceConfig.from_file(str(conf_file))

    assert config.retryable_status_codes == [429, 500, 502, 503]


def test_config_default_retryable_status_codes():
    """
    Test default values for retryable_status_codes.
    """
    config = ModelServiceConfig()

    assert config.retryable_status_codes == [429, 500]


@pytest.mark.asyncio
async def test_perform_llm_request_respects_custom_retryable_codes():
    """
    Test that custom retryable_status_codes are respected (502 retries, 401 does not).
    """
    config = ModelServiceConfig()
    config.retryable_status_codes = [502, 503, 504]  # Custom retryable status codes

    client_post_path = "rock.sdk.model.server.api.proxy.http_client.post"

    with patch(client_post_path, new_callable=AsyncMock) as mock_post, patch(
        "rock.utils.retry.asyncio.sleep", return_value=None
    ):
        # 502 should retry (in custom list)
        resp_502 = MagicMock(spec=Response)
        resp_502.status_code = 502
        error_502 = HTTPStatusError("Bad Gateway", request=MagicMock(spec=Request), response=resp_502)

        resp_200 = MagicMock(spec=Response)
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}

        # Sequence: 502 fail, then 200 success
        mock_post.side_effect = [error_502, resp_200]

        result = await perform_llm_request("http://fake.url", {}, {}, config)

        assert result.status_code == 200
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_perform_llm_request_non_retryable_code_not_retried():
    """
    Test that 401 (not in custom retryable_status_codes) does not trigger retry.
    """
    config = ModelServiceConfig()
    config.retryable_status_codes = [502, 503, 504]  # Custom retryable status codes, excluding 401

    client_post_path = "rock.sdk.model.server.api.proxy.http_client.post"

    with patch(client_post_path, new_callable=AsyncMock) as mock_post:
        # 401 should not retry (not in custom list)
        resp_401 = MagicMock(spec=Response)
        resp_401.status_code = 401
        resp_401.json.return_value = {"error": "Invalid API Key"}

        mock_post.return_value = resp_401

        result = await perform_llm_request("http://fake.url", {}, {}, config)

        assert result.status_code == 401
        assert mock_post.call_count == 1  # No retry


def test_cli_args_override_config_file(tmp_path):
    """
    Test that CLI arguments override config file settings.
    This tests the logic in create_config_from_args().
    """
    import argparse

    # Create args with config file and CLI parameters
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump(
            {
                "host": "192.168.1.1",
                "port": 8080,
                "proxy_base_url": "https://config-url.example.com/v1",
                "retryable_status_codes": [429, 500],
                "request_timeout": 60,
            }
        )
    )

    args = argparse.Namespace(
        config_file=str(conf_file),
        host="0.0.0.0",  # CLI overrides config file
        port=9000,  # CLI overrides config file
        proxy_base_url="https://cli-url.example.com/v1",  # CLI overrides config file
        retryable_status_codes="502,503",  # CLI overrides config file
        request_timeout=30,  # CLI overrides config file
    )

    config = create_config_from_args(args)

    # Verify CLI arguments override config file
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.proxy_base_url == "https://cli-url.example.com/v1"
    assert config.retryable_status_codes == [502, 503]
    assert config.request_timeout == 30


@pytest.mark.asyncio
async def test_config_file_overrides_defaults(tmp_path):
    """
    Test that config file values override default values.
    """
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump(
            {
                "host": "10.0.0.1",
                "port": 8888,
                "request_timeout": 300,
                "proxy_rules": {"test-model": "http://test-backend"},
            }
        )
    )

    config = ModelServiceConfig.from_file(str(conf_file))

    # Verify config file overrides defaults
    assert config.host == "10.0.0.1"
    assert config.port == 8888
    assert config.request_timeout == 300
    assert config.proxy_rules["test-model"] == "http://test-backend"
    # Verify other fields remain as defaults
    assert config.proxy_base_url is None


def test_metrics_monitor_is_singleton():
    """
    Test that _get_or_create_metrics_monitor returns the same instance
    on repeated calls (module-level singleton, created only once).
    """
    import rock.sdk.model.server.utils as utils_module

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls:
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor

        # Reset singleton so the test is isolated
        utils_module._metrics_monitor = None

        first = _get_or_create_metrics_monitor()
        second = _get_or_create_metrics_monitor()

        assert first is second
        assert mock_cls.create.call_count == 1

        # Cleanup
        utils_module._metrics_monitor = None


def test_metrics_monitor_uses_env_endpoint():
    """
    Test that ROCK_METRICS_ENDPOINT env var is passed to MetricsMonitor.create().
    """
    import rock.sdk.model.server.utils as utils_module

    custom_endpoint = "http://my-otel-collector:4318/v1/metrics"

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls, patch.dict(
        "os.environ", {"ROCK_METRICS_ENDPOINT": custom_endpoint}
    ):
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor

        utils_module._metrics_monitor = None
        _get_or_create_metrics_monitor()

        mock_cls.create.assert_called_once_with(metrics_endpoint=custom_endpoint)

        utils_module._metrics_monitor = None


def test_metrics_monitor_registers_gauge_and_counter():
    """
    Test that _get_or_create_metrics_monitor registers both
    the RT gauge and request count counter on first creation.
    """
    import rock.sdk.model.server.utils as utils_module

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls:
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor

        utils_module._metrics_monitor = None
        _get_or_create_metrics_monitor()

        mock_monitor._register_gauge.assert_called_once_with(
            MODEL_SERVICE_REQUEST_RT, "total execution time for request", "ms"
        )
        mock_monitor._register_counter.assert_called_once_with(
            MODEL_SERVICE_REQUEST_COUNT, "total request count", "count"
        )

        utils_module._metrics_monitor = None


@pytest.mark.asyncio
async def test_record_traj_reports_rt_and_count():
    """
    Test that record_traj decorator calls record_gauge_by_name (RT)
    and record_counter_by_name (count) with correct metric names and attributes.
    """
    import rock.sdk.model.server.utils as utils_module

    mock_monitor = MagicMock()

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls, patch.dict(
        "os.environ", {"ROCK_SANDBOX_ID": "sandbox-test-001"}
    ):
        mock_cls.create.return_value = mock_monitor
        utils_module._metrics_monitor = None

        @record_traj
        async def fake_handler(body: dict):
            return {"id": "resp-1", "choices": []}

        await fake_handler({"model": "gpt-4", "messages": []})

        mock_monitor.record_gauge_by_name.assert_called_once()
        gauge_call = mock_monitor.record_gauge_by_name.call_args
        assert gauge_call[0][0] == MODEL_SERVICE_REQUEST_RT
        assert gauge_call[1]["attributes"]["type"] == "chat_completions"
        assert gauge_call[1]["attributes"]["sandbox_id"] == "sandbox-test-001"

        mock_monitor.record_counter_by_name.assert_called_once()
        counter_call = mock_monitor.record_counter_by_name.call_args
        assert counter_call[0][0] == MODEL_SERVICE_REQUEST_COUNT
        assert counter_call[0][1] == 1
        assert counter_call[1]["attributes"]["sandbox_id"] == "sandbox-test-001"

        utils_module._metrics_monitor = None


@pytest.mark.asyncio
async def test_record_traj_sandbox_id_defaults_to_unknown():
    """
    Test that sandbox_id defaults to 'unknown' when ROCK_SANDBOX_ID is not set.
    """
    import rock.sdk.model.server.utils as utils_module

    mock_monitor = MagicMock()

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls, patch.dict("os.environ", {}, clear=False):
        # Ensure ROCK_SANDBOX_ID is not set
        os_env = __import__("os").environ
        os_env.pop("ROCK_SANDBOX_ID", None)

        mock_cls.create.return_value = mock_monitor
        utils_module._metrics_monitor = None

        @record_traj
        async def fake_handler(body: dict):
            return {"id": "resp-2", "choices": []}

        await fake_handler({"model": "gpt-4", "messages": []})

        gauge_call = mock_monitor.record_gauge_by_name.call_args
        assert gauge_call[1]["attributes"]["sandbox_id"] == "unknown"

        utils_module._metrics_monitor = None
