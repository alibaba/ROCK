"""End-to-end: real in-process TCP mock upstream + real proxy router + recorder.

The mock upstream is a tiny FastAPI app served by uvicorn in a background thread
(real TCP). The proxy stays in-process and is hit via FastAPI's ``TestClient``;
its outbound ``httpx.AsyncClient`` makes a real TCP call to the mock — production
code path, no transport injection, no patching.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from rock.sdk.model.server.api.proxy import ForwardBackend, ReplayBackend, proxy_router
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.integrations.traj_recorder import TrajectoryRecorder
from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor
from rock.utils.system import find_free_port

# ---------- Mock upstream: a tiny FastAPI app behind a real TCP uvicorn ----------


def _build_mock_upstream() -> FastAPI:
    """One stream + one non-stream reply, with a vendor field to verify byte-passthrough."""
    app = FastAPI()

    def completion(model: str) -> dict:
        return {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello, ROCK!",
                        "reasoning_content": "thinking step-by-step",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }

    async def stream_gen(model: str):
        base = {"id": "chatcmpl-mock-1", "object": "chat.completion.chunk", "created": 0, "model": model}
        for piece in ["Hello", ", ", "ROCK", "!"]:
            chunk = {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(0.005)
        final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        model = body.get("model", "mock")
        if body.get("stream"):
            return StreamingResponse(stream_gen(model), media_type="text/event-stream")
        return JSONResponse(status_code=200, content=completion(model))

    return app


class MockUpstreamServer:
    """Runs ``_build_mock_upstream()`` behind a real TCP uvicorn in a background thread.

    Use as ``with MockUpstreamServer() as base_url: ...``. ``Server.run()``
    spins up its own asyncio loop inside the thread; we poll ``server.started``
    to know when it's accepting connections.
    """

    def __init__(self) -> None:
        port = asyncio.run(find_free_port())
        config = uvicorn.Config(
            _build_mock_upstream(), host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}/v1"

    def __enter__(self) -> str:
        self._thread.start()
        deadline = time.time() + 5.0
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("mock upstream did not start within 5s")
            time.sleep(0.02)
        return self.base_url

    def __exit__(self, *_exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def mock_upstream() -> Iterator[str]:
    with MockUpstreamServer() as base_url:
        yield base_url


# ---------- Proxy app builder + tests ----------


def _build_proxy_app(*, mock_url: str, traj_file: Path | None = None, replay_cursor=None) -> FastAPI:
    config = ModelServiceConfig()
    config.proxy_base_url = mock_url

    app = FastAPI()
    app.state.model_service_config = config
    if replay_cursor is not None:
        app.state.backend = ReplayBackend(replay_cursor)
    else:
        recorder = TrajectoryRecorder(traj_file=traj_file) if traj_file is not None else None
        app.state.backend = ForwardBackend(config, recorder=recorder)
    app.include_router(proxy_router)
    return app


def test_e2e_non_stream_forwards_and_records(mock_upstream, tmp_path):
    """Vendor field reaches the client; recorder writes a JSONL line with the full response."""
    traj_file = tmp_path / "traj.jsonl"
    proxy_app = _build_proxy_app(mock_url=mock_upstream, traj_file=traj_file)

    with TestClient(proxy_app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-key"},
        )

    assert r.status_code == 200
    msg = r.json()["choices"][0]["message"]
    assert msg["content"] == "Hello, ROCK!"
    assert msg["reasoning_content"] == "thinking step-by-step"

    rec = json.loads(traj_file.read_text(encoding="utf-8").strip())
    assert rec["status"] == "success"
    assert rec["stream"] is False
    assert rec["response"]["choices"][0]["message"]["reasoning_content"] == "thinking step-by-step"


def test_e2e_stream_forwards_chunks_and_records_aggregated(mock_upstream, tmp_path):
    """Each upstream SSE chunk reaches the client; recorder gets the aggregated final completion."""
    traj_file = tmp_path / "traj.jsonl"
    proxy_app = _build_proxy_app(mock_url=mock_upstream, traj_file=traj_file)

    with TestClient(proxy_app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-key"},
        ) as r:
            body = b"".join(r.iter_bytes()).decode("utf-8")

    for piece in ["Hello", "ROCK"]:
        assert f'"content": "{piece}"' in body
    assert '"finish_reason": "stop"' in body
    assert body.rstrip().endswith("data: [DONE]")

    rec = json.loads(traj_file.read_text(encoding="utf-8").strip())
    assert rec["status"] == "success"
    assert rec["stream"] is True
    assert rec["response"]["choices"][0]["message"]["content"] == "Hello, ROCK!"


def test_e2e_record_then_replay_returns_same_content(mock_upstream, tmp_path):
    """Record one non-stream + one stream call, then replay them without touching the upstream."""
    traj_file = tmp_path / "traj.jsonl"

    # ---- record phase ----
    proxy_record = _build_proxy_app(mock_url=mock_upstream, traj_file=traj_file)
    with TestClient(proxy_record) as client:
        r1 = client.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as st:
            for _ in st.iter_bytes():
                pass
    recorded = r1.json()

    # ---- replay phase: bogus base_url proves the upstream isn't called ----
    cursor = SequentialCursor.load(traj_file)
    proxy_replay = _build_proxy_app(mock_url="http://invalid.local:1/v1", replay_cursor=cursor)
    with TestClient(proxy_replay) as client:
        ns2 = client.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "different"}]},
        )
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "different"}]},
        ) as st:
            st_body = b"".join(st.iter_bytes()).decode("utf-8")

    assert ns2.status_code == 200
    assert ns2.json()["choices"][0]["message"]["content"] == recorded["choices"][0]["message"]["content"]
    assert "Hello, ROCK!" in st_body
    assert st_body.rstrip().endswith("data: [DONE]")
