import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.sdk.model.server.api.proxy import proxy_router
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


# Patch path for the litellm.acompletion symbol as imported inside proxy.py.
ACOMPLETION_PATCH = "rock.sdk.model.server.api.proxy.litellm.acompletion"


def _fake_model_response(*, id="chat-123", choices=None) -> SimpleNamespace:
    """Build a litellm-shaped object that exposes .model_dump() like a Pydantic model."""
    payload = {
        "id": id,
        "object": "chat.completion",
        "model": "gpt-3.5-turbo",
        "choices": choices
        or [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return SimpleNamespace(model_dump=lambda: payload)


@pytest.mark.asyncio
async def test_chat_completions_routing_success():
    """Routing: model name maps to its proxy_rules entry, passed to litellm as api_base."""
    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = _fake_model_response()

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        assert mock_acompletion.called
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["api_base"] == "https://api.openai.com/v1"
        assert call_kwargs["model"] == "custom_openai/gpt-3.5-turbo"
        assert call_kwargs["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_chat_completions_fallback_to_default_when_not_found():
    """Unrecognized model name → falls back to the 'default' base URL."""
    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = _fake_model_response(id="chat-fallback")

        config = test_app.state.model_service_config
        default_base_url = config.proxy_rules["default"].rstrip("/")

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "model": "some-random-unsupported-model",
                "messages": [{"role": "user", "content": "hello"}],
            }
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["api_base"] == default_base_url


@pytest.mark.asyncio
async def test_chat_completions_routing_absolute_fail():
    """No matching rule and no 'default' → 400."""
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
async def test_proxy_base_url_overrides_proxy_rules():
    """When proxy_base_url is set, all requests go to that URL, ignoring proxy_rules."""
    config = ModelServiceConfig()
    config.proxy_base_url = "https://custom-endpoint.example.com/v1"

    local_app = FastAPI()
    local_app.state.model_service_config = config
    local_app.include_router(proxy_router)

    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = _fake_model_response()

        transport = ASGITransport(app=local_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

        assert response.status_code == 200
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["api_base"] == "https://custom-endpoint.example.com/v1"


@pytest.mark.asyncio
async def test_chat_completions_passes_num_retries_and_timeout():
    """num_retries and request_timeout from config flow through to litellm.acompletion."""
    config = ModelServiceConfig()
    config.num_retries = 3
    config.request_timeout = 45

    local_app = FastAPI()
    local_app.state.model_service_config = config
    local_app.include_router(proxy_router)

    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = _fake_model_response()

        transport = ASGITransport(app=local_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]}
            await ac.post("/v1/chat/completions", json=payload)

        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["num_retries"] == 3
        assert call_kwargs["timeout"] == 45


@pytest.mark.asyncio
async def test_chat_completions_litellm_error_returns_proxy_schema():
    """A litellm exception is converted to {error:{message,type,code}} JSON
    so agent-side keyword detection (e.g. 'context length exceeded') keeps working."""
    from litellm.exceptions import BadRequestError

    err = BadRequestError(
        message="context length exceeded for this model",
        model="gpt-3.5-turbo",
        llm_provider="openai",
    )

    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.side_effect = err

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

        body = response.json()
        assert "error" in body
        assert "context length exceeded" in body["error"]["message"]
        assert body["error"]["type"] == "BadRequestError"
        assert body["error"]["code"] == response.status_code


@pytest.mark.asyncio
async def test_replay_mode_returns_recorded_response_without_calling_litellm(tmp_path):
    """In replay mode the proxy serves the next record directly from app.state.replay_cursor;
    litellm.acompletion must never be invoked."""
    from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor

    record = {
        "model": "gpt-3.5-turbo",
        "response": {
            "id": "rec-1",
            "object": "chat.completion",
            "model": "gpt-3.5-turbo",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "recorded reply"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    }
    traj = tmp_path / "t.jsonl"
    traj.write_text(json.dumps(record) + "\n", encoding="utf-8")

    config = ModelServiceConfig()
    config.replay_traj_path = str(traj)

    local_app = FastAPI()
    local_app.state.model_service_config = config
    local_app.state.replay_cursor = SequentialCursor.load(traj)
    local_app.include_router(proxy_router)

    with patch(ACOMPLETION_PATCH, new_callable=AsyncMock) as mock_acompletion:
        transport = ASGITransport(app=local_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]}
            response = await ac.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "recorded reply"
    mock_acompletion.assert_not_called()


@pytest.mark.asyncio
async def test_replay_mode_streaming_emits_recorded_response_as_sse(tmp_path):
    """Replay + stream=True emits one SSE chunk (content moved into delta) plus [DONE]."""
    from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor

    record = {
        "model": "gpt-3.5-turbo",
        "response": {
            "id": "rec-stream",
            "object": "chat.completion",
            "model": "gpt-3.5-turbo",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "streamed reply"},
                    "finish_reason": "tool_calls",
                }
            ],
        },
    }
    traj = tmp_path / "t.jsonl"
    traj.write_text(json.dumps(record) + "\n", encoding="utf-8")

    config = ModelServiceConfig()
    config.replay_traj_path = str(traj)

    local_app = FastAPI()
    local_app.state.model_service_config = config
    local_app.state.replay_cursor = SequentialCursor.load(traj)
    local_app.include_router(proxy_router)

    transport = ASGITransport(app=local_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    body = response.text
    assert "data: [DONE]" in body
    # The SSE chunk shape is chat.completion.chunk with message → delta, finish_reason preserved
    assert '"object": "chat.completion.chunk"' in body
    assert '"delta": {"role": "assistant", "content": "streamed reply"}' in body
    assert '"finish_reason": "tool_calls"' in body


@pytest.mark.asyncio
async def test_replay_mode_returns_404_when_cursor_exhausted(tmp_path):
    """Cursor used up → 404 with a clear message; no litellm retries involved."""
    from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor

    record = {
        "model": "gpt-3.5-turbo",
        "response": {
            "id": "only",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        },
    }
    traj = tmp_path / "t.jsonl"
    traj.write_text(json.dumps(record) + "\n", encoding="utf-8")

    config = ModelServiceConfig()
    config.replay_traj_path = str(traj)

    local_app = FastAPI()
    local_app.state.model_service_config = config
    local_app.state.replay_cursor = SequentialCursor.load(traj)
    local_app.include_router(proxy_router)

    transport = ASGITransport(app=local_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
        )
        second = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "again"}]},
        )

    assert second.status_code == 404
    assert "exhausted" in second.json()["detail"]


@pytest.mark.asyncio
async def test_chat_completions_extracts_bearer_token_and_strips_framing_headers():
    """Bearer token goes to api_key kwarg; host / content-length / transfer-encoding /
    Authorization are not forwarded as extra_headers (litellm regenerates Authorization
    from api_key, so passing it both ways would conflict). Custom X-* headers pass through."""
    captured = {}

    async def capture(*args, **kwargs):
        captured.update(kwargs)
        return _fake_model_response()

    with patch(ACOMPLETION_PATCH, new=capture):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]}
            await ac.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer abc", "X-Trace": "t1"},
            )

    assert captured["api_key"] == "abc"

    forwarded = captured["extra_headers"]
    forwarded_lower = {k.lower() for k in forwarded}
    assert "x-trace" in forwarded_lower
    assert "authorization" not in forwarded_lower
    assert "host" not in forwarded_lower
    assert "content-length" not in forwarded_lower
    assert "content-type" not in forwarded_lower
    assert "transfer-encoding" not in forwarded_lower


@pytest.mark.asyncio
async def test_lifespan_initialization_with_config(tmp_path):
    """Application initializes correctly when a valid config file is provided."""
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"proxy_rules": {"my-model": "http://custom-url"}, "request_timeout": 50}))

    config = ModelServiceConfig.from_file(str(conf_file))
    app = FastAPI(lifespan=lambda app: lifespan(app, config))

    async with lifespan(app, config):
        app_config = app.state.model_service_config
        assert app_config.proxy_rules["my-model"] == "http://custom-url"
        assert app_config.request_timeout == 50
        assert "gpt-3.5-turbo" not in app_config.proxy_rules


@pytest.mark.asyncio
async def test_lifespan_initialization_no_config():
    """Defaults are loaded when no config file is provided."""
    config = ModelServiceConfig()
    app = FastAPI(lifespan=lambda app: lifespan(app, config))

    async with lifespan(app, config):
        app_config = app.state.model_service_config
        assert "gpt-3.5-turbo" in app_config.proxy_rules
        assert app_config.request_timeout == 120


@pytest.mark.asyncio
async def test_lifespan_invalid_config_path():
    """Non-existent config path → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        ModelServiceConfig.from_file("/tmp/non_existent_file.yml")


@pytest.mark.asyncio
async def test_config_loads_host_and_port_from_file(tmp_path):
    """ModelServiceConfig loads host and port from config file."""
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump({"host": "127.0.0.1", "port": 9000, "proxy_rules": {"my-model": "http://my-backend"}})
    )

    config = ModelServiceConfig.from_file(str(conf_file))

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.proxy_rules["my-model"] == "http://my-backend"


def test_config_default_host_and_port():
    config = ModelServiceConfig()
    assert config.host == "0.0.0.0"
    assert config.port == 8080


@pytest.mark.asyncio
async def test_config_loads_retryable_status_codes_from_file(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"retryable_status_codes": [429, 500, 502, 503]}))

    config = ModelServiceConfig.from_file(str(conf_file))
    assert config.retryable_status_codes == [429, 500, 502, 503]


def test_config_default_retryable_status_codes():
    config = ModelServiceConfig()
    assert config.retryable_status_codes == [429, 500]


def test_config_default_traj_and_replay():
    """New traj/replay defaults: recording on (append=True), replay off."""
    config = ModelServiceConfig()
    assert config.traj_enabled is True
    assert config.traj_file is None
    assert config.replay_traj_path is None
    assert config.num_retries == 6


@pytest.mark.asyncio
async def test_config_loads_traj_and_replay_from_file(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump(
            {
                "traj_enabled": False,
                "traj_file": "/tmp/my-traj.jsonl",
                "replay_traj_path": "/tmp/in.jsonl",
                "num_retries": 2,
            }
        )
    )

    config = ModelServiceConfig.from_file(str(conf_file))
    assert config.traj_enabled is False
    assert config.traj_file == "/tmp/my-traj.jsonl"
    assert config.replay_traj_path == "/tmp/in.jsonl"
    assert config.num_retries == 2


def test_cli_args_override_config_file(tmp_path):
    """CLI arguments override config file settings."""
    import argparse

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
        host="0.0.0.0",
        port=9000,
        proxy_base_url="https://cli-url.example.com/v1",
        retryable_status_codes="502,503",
        request_timeout=30,
        num_retries=4,
        traj_file=None,
    )

    config = create_config_from_args(args)

    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.proxy_base_url == "https://cli-url.example.com/v1"
    assert config.retryable_status_codes == [502, 503]
    assert config.request_timeout == 30
    assert config.num_retries == 4


def test_cli_traj_file_enables_replay():
    """--traj-file sets replay_enabled, replay_traj_path, and disables recording."""
    import argparse

    args = argparse.Namespace(
        config_file=None,
        host=None,
        port=None,
        proxy_base_url=None,
        retryable_status_codes=None,
        request_timeout=None,
        num_retries=None,
        traj_file="/tmp/in.jsonl",
    )

    config = create_config_from_args(args)
    assert config.replay_traj_path == "/tmp/in.jsonl"
    assert config.traj_enabled is False


@pytest.mark.asyncio
async def test_config_file_overrides_defaults(tmp_path):
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

    assert config.host == "10.0.0.1"
    assert config.port == 8888
    assert config.request_timeout == 300
    assert config.proxy_rules["test-model"] == "http://test-backend"
    assert config.proxy_base_url is None


def test_metrics_monitor_is_singleton():
    """_get_or_create_metrics_monitor returns the same instance on repeated calls."""
    import rock.sdk.model.server.utils as utils_module

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls:
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor
        utils_module._metrics_monitor = None

        first = _get_or_create_metrics_monitor()
        second = _get_or_create_metrics_monitor()

        assert first is second
        assert mock_cls.create.call_count == 1
        utils_module._metrics_monitor = None


def test_metrics_monitor_uses_env_endpoint():
    """ROCK_METRICS_ENDPOINT env var is passed to MetricsMonitor.create()."""
    import rock.sdk.model.server.utils as utils_module

    custom_endpoint = "http://my-otel-collector:4318/v1/metrics"

    with (
        patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls,
        patch.dict("os.environ", {"ROCK_METRICS_ENDPOINT": custom_endpoint}),
    ):
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor
        utils_module._metrics_monitor = None
        _get_or_create_metrics_monitor()
        mock_cls.create.assert_called_once_with(metrics_endpoint=custom_endpoint)
        utils_module._metrics_monitor = None


def test_metrics_monitor_registers_gauge_and_counter():
    """_get_or_create_metrics_monitor registers both metrics on first creation."""
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
    """Legacy record_traj decorator (still used by local mode) reports RT/count."""
    import rock.sdk.model.server.utils as utils_module

    mock_monitor = MagicMock()

    with (
        patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls,
        patch.dict("os.environ", {"ROCK_SANDBOX_ID": "sandbox-test-001"}),
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
    """sandbox_id defaults to 'unknown' when ROCK_SANDBOX_ID is not set."""
    import rock.sdk.model.server.utils as utils_module

    mock_monitor = MagicMock()

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls, patch.dict("os.environ", {}, clear=False):
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
