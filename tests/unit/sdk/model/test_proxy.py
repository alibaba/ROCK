"""Tests for the chat/completions proxy.

Forward path is exercised by pointing the proxy at an httpx ``MockTransport``
(no real network). Replay path is exercised end-to-end via the FastAPI test
client. Config / CLI / metrics-singleton tests round out the file.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.sdk.model.server.api.proxy import proxy_router
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.main import create_config_from_args, lifespan
from rock.sdk.model.server.traj import SequentialCursor
from rock.sdk.model.server.utils import (
    MODEL_SERVICE_REQUEST_COUNT,
    MODEL_SERVICE_REQUEST_RT,
    _get_or_create_metrics_monitor,
    record_traj,
)


def _build_app(config: ModelServiceConfig, *, replay_cursor=None, recorder=None) -> FastAPI:
    """Build a FastAPI app with the proxy router and the given config attached."""
    from rock.sdk.model.server.api.proxy import ForwardBackend, ReplayBackend

    app = FastAPI()
    app.state.model_service_config = config
    if replay_cursor is not None:
        app.state.backend = ReplayBackend(replay_cursor)
    else:
        app.state.backend = ForwardBackend(config, recorder=recorder)
    app.include_router(proxy_router)
    return app


def _patch_httpx_with_handler(handler):
    """Patch ``proxy.httpx.AsyncClient`` so each ``async with httpx.AsyncClient(...)``
    returns a real client wrapping ``MockTransport(handler)``."""
    real_client_cls = httpx.AsyncClient  # capture before patching kicks in
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)  # transport supplies the response, no timeout needed
        return real_client_cls(transport=transport, **kwargs)

    return patch("rock.sdk.model.server.api.proxy.httpx.AsyncClient", side_effect=factory)


def _success_response_json(*, model: str = "gpt-3.5-turbo", content: str = "hi") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1234,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ---------- Forward path: routing ----------


@pytest.mark.asyncio
async def test_forward_routes_by_model_name_to_proxy_rules():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_success_response_json())

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 200
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["body"]["model"] == "gpt-3.5-turbo"


@pytest.mark.asyncio
async def test_forward_falls_back_to_default_for_unknown_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_success_response_json(model="some-random"))

    config = ModelServiceConfig()
    expected_default = config.proxy_rules["default"].rstrip("/") + "/chat/completions"
    app = _build_app(config)

    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "some-random", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 200
    assert captured["url"] == expected_default


@pytest.mark.asyncio
async def test_forward_400_when_no_rule_and_no_default():
    config = ModelServiceConfig()
    config.proxy_rules = {}
    app = _build_app(config)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


@pytest.mark.asyncio
async def test_forward_proxy_base_url_overrides_proxy_rules():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_success_response_json())

    config = ModelServiceConfig()
    config.proxy_base_url = "https://custom-endpoint.example.com/v1"
    app = _build_app(config)

    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert captured["url"] == "https://custom-endpoint.example.com/v1/chat/completions"


# ---------- Forward path: byte passthrough ----------


@pytest.mark.asyncio
async def test_forward_response_body_is_byte_for_byte_passthrough():
    """Upstream's exact JSON bytes (incl. provider-specific fields) reach the client."""
    upstream_payload = {
        "id": "x",
        "object": "chat.completion",
        "model": "glm-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi", "reasoning_content": "...think..."},
                "finish_reason": "stop",
            }
        ],
        "provider_specific_fields": {"vendor_field": "vendor_value"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_payload)

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]},
            )

    body = r.json()
    assert body["choices"][0]["message"]["reasoning_content"] == "...think..."
    assert body["provider_specific_fields"] == {"vendor_field": "vendor_value"}


@pytest.mark.asyncio
async def test_forward_propagates_upstream_status_and_body_on_4xx():
    """Upstream 4xx is forwarded verbatim — proxy doesn't re-shape error JSON."""
    err_body = {"error": {"message": "context length exceeded", "type": "BadRequestError"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=err_body)

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 400
    assert r.json() == err_body


@pytest.mark.asyncio
async def test_forward_authorization_header_passes_through():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_success_response_json())

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer sk-abc", "X-Trace": "t1"},
            )

    # Authorization and custom X-* headers are forwarded verbatim. We don't assert
    # on framing headers (connection / content-length / accept-encoding) because
    # httpx rebuilds them itself for the outgoing request.
    auth_value = captured["headers"].get("Authorization") or captured["headers"].get("authorization")
    assert auth_value == "Bearer sk-abc"
    fwd_lower = {k.lower() for k in captured["headers"]}
    assert "x-trace" in fwd_lower


@pytest.mark.asyncio
async def test_forward_502_on_upstream_connection_failure(monkeypatch):
    """ConnectError → 502. Retry disabled here to keep the test fast."""
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_MAX_ATTEMPTS", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream is down")

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 502


# ---------- Forward path: retry ----------


@pytest.mark.asyncio
async def test_forward_retries_on_retryable_status_then_succeeds(monkeypatch):
    """A 429 is retried; the next attempt's 200 is returned to the client."""
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_DELAY_SECONDS", 0.0)

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=_success_response_json(content="finally"))

    app = _build_app(ModelServiceConfig())  # default retryable_status_codes = [429, 500]
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "finally"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_forward_returns_last_response_when_retries_exhausted(monkeypatch):
    """All attempts return 429 → the final 429 body+status is forwarded verbatim."""
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_DELAY_SECONDS", 0.0)

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, json={"error": "still rate limited"})

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 429
    assert r.json() == {"error": "still rate limited"}
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_forward_does_not_retry_non_whitelisted_status(monkeypatch):
    """400 is not in retryable_status_codes → forwarded immediately, no retry."""
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_DELAY_SECONDS", 0.0)

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert r.status_code == 400
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_forward_stream_retries_on_retryable_status_then_succeeds(monkeypatch):
    """Streaming: 500 on first attempt, then 200 SSE on second — client sees only the 200 body."""
    monkeypatch.setattr("rock.sdk.model.server.api.proxy._RETRY_DELAY_SECONDS", 0.0)

    attempts = []
    sse_body = (
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
        b'"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    app = _build_app(ModelServiceConfig())
    with _patch_httpx_with_handler(handler):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

    body = r.text
    assert "hello" in body
    assert "[DONE]" in body
    assert "internal" not in body  # the 500 attempt is not leaked to client
    assert len(attempts) == 2


# ---------- Forward path: recording ----------


@pytest.mark.asyncio
async def test_forward_invokes_recorder_on_success(tmp_path):
    """When a recorder is attached to the backend, success calls write a JSONL line."""
    from rock.sdk.model.server.traj import TrajectoryRecorder

    upstream_payload = _success_response_json(content="recorded reply")
    traj_file = tmp_path / "traj.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_payload)

    config = ModelServiceConfig()

    with _patch_httpx_with_handler(handler):
        recorder = TrajectoryRecorder(traj_file=traj_file)
        app = _build_app(config, recorder=recorder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
            )

    line = traj_file.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["status"] == "success"
    assert record["model"] == "gpt-3.5-turbo"
    assert record["stream"] is False
    assert record["request"]["messages"][0]["content"] == "hi"
    assert record["response"] == upstream_payload


# ---------- Replay path ----------


@pytest.mark.asyncio
async def test_replay_returns_recorded_response_no_upstream_call(tmp_path):
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
        },
    }
    traj = tmp_path / "t.jsonl"
    traj.write_text(json.dumps(record) + "\n", encoding="utf-8")

    config = ModelServiceConfig()
    config.replay_file = str(traj)
    app = _build_app(config, replay_cursor=SequentialCursor.load(traj))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "recorded reply"


@pytest.mark.asyncio
async def test_replay_streaming_emits_recorded_response_as_sse(tmp_path):
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
    config.replay_file = str(traj)
    app = _build_app(config, replay_cursor=SequentialCursor.load(traj))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )

    body = r.text
    assert "data: [DONE]" in body
    assert '"object": "chat.completion.chunk"' in body
    assert '"delta": {"role": "assistant", "content": "streamed reply"}' in body
    assert '"finish_reason": "tool_calls"' in body


@pytest.mark.asyncio
async def test_replay_returns_404_when_cursor_exhausted(tmp_path):
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
    config.replay_file = str(traj)
    app = _build_app(config, replay_cursor=SequentialCursor.load(traj))

    transport = ASGITransport(app=app)
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


# ---------- Lifespan + Config ----------


@pytest.mark.asyncio
async def test_lifespan_initialization_with_config(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"proxy_rules": {"my-model": "http://custom-url"}, "request_timeout": 50}))

    config = ModelServiceConfig.from_file(str(conf_file))
    app = FastAPI(lifespan=lambda app: lifespan(app, config))

    async with lifespan(app, config):
        assert app.state.model_service_config.proxy_rules["my-model"] == "http://custom-url"
        assert app.state.model_service_config.request_timeout == 50


@pytest.mark.asyncio
async def test_lifespan_invalid_config_path():
    with pytest.raises(FileNotFoundError):
        ModelServiceConfig.from_file("/tmp/non_existent_file.yml")


def test_config_default_host_and_port():
    config = ModelServiceConfig()
    assert config.host == "0.0.0.0"
    assert config.port == 8080


def test_config_default_recording_and_replay():
    config = ModelServiceConfig()
    assert config.recording_file is None
    assert config.replay_file is None


@pytest.mark.asyncio
async def test_config_loads_recording_file_from_yaml(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"recording_file": "/tmp/my-traj.jsonl"}))
    config = ModelServiceConfig.from_file(str(conf_file))
    assert config.recording_file == "/tmp/my-traj.jsonl"
    assert config.replay_file is None


@pytest.mark.asyncio
async def test_config_loads_replay_file_from_yaml(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(yaml.dump({"replay_file": "/tmp/in.jsonl"}))
    config = ModelServiceConfig.from_file(str(conf_file))
    assert config.replay_file == "/tmp/in.jsonl"
    assert config.recording_file is None


def test_config_recording_and_replay_are_mutually_exclusive():
    """Setting both at construction time fails Pydantic validation."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        ModelServiceConfig(recording_file="/tmp/a.jsonl", replay_file="/tmp/b.jsonl")


def test_config_recording_replay_mutex_fires_on_assignment():
    """validate_assignment=True so CLI-style field-by-field overrides also trip the mutex."""
    config = ModelServiceConfig(recording_file="/tmp/a.jsonl")
    with pytest.raises(ValueError, match="mutually exclusive"):
        config.replay_file = "/tmp/b.jsonl"


def test_cli_args_override_config_file(tmp_path):
    conf_file = tmp_path / "proxy.yml"
    conf_file.write_text(
        yaml.dump(
            {
                "host": "192.168.1.1",
                "port": 8080,
                "proxy_base_url": "https://config-url.example.com/v1",
                "request_timeout": 60,
            }
        )
    )
    args = argparse.Namespace(
        config_file=str(conf_file),
        host="0.0.0.0",
        port=9000,
        proxy_base_url="https://cli-url.example.com/v1",
        retryable_status_codes=None,
        request_timeout=30,
        recording_file=None,
        replay_file=None,
    )
    config = create_config_from_args(args)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.proxy_base_url == "https://cli-url.example.com/v1"
    assert config.request_timeout == 30


def test_cli_replay_file_enables_replay():
    args = argparse.Namespace(
        config_file=None,
        host=None,
        port=None,
        proxy_base_url=None,
        retryable_status_codes=None,
        request_timeout=None,
        recording_file=None,
        replay_file="/tmp/in.jsonl",
    )
    config = create_config_from_args(args)
    assert config.replay_file == "/tmp/in.jsonl"


# ---------- Metrics singleton + legacy record_traj (still used by local mode) ----------


def test_metrics_monitor_is_singleton():
    import rock.sdk.model.server.utils as utils_module

    with patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls:
        mock_cls.create.return_value = MagicMock()
        utils_module._metrics_monitor = None
        first = _get_or_create_metrics_monitor()
        second = _get_or_create_metrics_monitor()
        assert first is second
        utils_module._metrics_monitor = None


@pytest.mark.asyncio
async def test_record_traj_decorator_reports_rt_and_count():
    """Legacy record_traj decorator (still used by local mode) reports RT/count."""
    import rock.sdk.model.server.utils as utils_module

    with (
        patch("rock.sdk.model.server.utils.MetricsMonitor") as mock_cls,
        patch.dict("os.environ", {"ROCK_SANDBOX_ID": "sandbox-test"}),
    ):
        mock_monitor = MagicMock()
        mock_cls.create.return_value = mock_monitor
        utils_module._metrics_monitor = None

        @record_traj
        async def fake_handler(body: dict):
            return {"id": "resp-1", "choices": []}

        await fake_handler({"model": "gpt-4", "messages": []})

        gauge_call = mock_monitor.record_gauge_by_name.call_args
        assert gauge_call[0][0] == MODEL_SERVICE_REQUEST_RT
        assert gauge_call[1]["attributes"]["sandbox_id"] == "sandbox-test"

        counter_call = mock_monitor.record_counter_by_name.call_args
        assert counter_call[0][0] == MODEL_SERVICE_REQUEST_COUNT

        utils_module._metrics_monitor = None
