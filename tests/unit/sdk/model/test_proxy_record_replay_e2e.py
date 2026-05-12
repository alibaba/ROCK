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
from rock.sdk.model.server.sse import parse_sse_data_chunks
from rock.sdk.model.server.traj import SequentialCursor, TrajectoryRecorder
from rock.utils.system import find_free_port

# ---------------------------------------------------------------------------
# Mock upstream — a tiny FastAPI app behind a real TCP uvicorn in a thread.
# Owns both the canned reply AND the assertion helper, so the response shape
# and the expectations stay in sync if either is edited.
# ---------------------------------------------------------------------------


class MockUpstream:
    """Mock OpenAI-compatible upstream.

    Single canonical reply (returned for both stream and non-stream requests)
    contains three fields the proxy must preserve end-to-end:
      - ``content``            (plain text)
      - ``reasoning_content``  (vendor-specific thinking)
      - ``tool_calls``         (a function call)
    The streaming variant splits each field into multiple deltas so the
    recorder also exercises the openai SDK's stream-state aggregator.

    Use as ``with MockUpstream() as mock: ...``; ``mock.base_url`` points at
    the running server. ``mock.assert_message(msg)`` checks any received
    assistant message matches the canonical reply.
    """

    # Canonical reply values — change here, both the handler and the assertion
    # helper pick them up automatically. Two parallel tool_calls cover the
    # multi-tool-call case (modern LLMs commonly emit several at once).
    EXPECTED_CONTENT = "Checking weather and time for you."
    EXPECTED_REASONING = "User wants weather + time; calling both tools in parallel."
    EXPECTED_TOOL_CALLS = [
        {
            "id": "call_weather",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Tokyo","unit":"celsius"}'},
        },
        {
            "id": "call_time",
            "type": "function",
            "function": {"name": "get_time", "arguments": '{"city":"Tokyo"}'},
        },
    ]

    def __init__(self) -> None:
        port = asyncio.run(find_free_port())
        config = uvicorn.Config(self._build_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}/v1"

    # ---- lifecycle ----

    def __enter__(self) -> MockUpstream:
        self._thread.start()
        deadline = time.time() + 5.0
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("mock upstream did not start within 5s")
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    # ---- assertion helper ----

    def assert_message(self, msg: dict) -> None:
        """Assert ``msg`` is the canonical full message (content + reasoning + 2 parallel tool_calls)."""
        assert msg["content"] == self.EXPECTED_CONTENT
        assert msg["reasoning_content"] == self.EXPECTED_REASONING
        tcs = msg["tool_calls"]
        assert len(tcs) == len(self.EXPECTED_TOOL_CALLS)
        for actual, expected in zip(tcs, self.EXPECTED_TOOL_CALLS, strict=True):
            assert actual["id"] == expected["id"]
            assert actual["type"] == expected["type"]
            assert actual["function"]["name"] == expected["function"]["name"]
            assert json.loads(actual["function"]["arguments"]) == json.loads(expected["function"]["arguments"])

    # ---- internal: FastAPI app + handlers ----

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            body = await request.json()
            model = body.get("model", "mock")
            if body.get("stream"):
                return StreamingResponse(self._stream_gen(model), media_type="text/event-stream")
            return JSONResponse(status_code=200, content=self._completion_json(model))

        return app

    def _completion_json(self, model: str) -> dict:
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
                        "content": self.EXPECTED_CONTENT,
                        "reasoning_content": self.EXPECTED_REASONING,
                        "tool_calls": self.EXPECTED_TOOL_CALLS,
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36},
        }

    async def _stream_gen(self, model: str):
        base = {"id": "chatcmpl-mock-1", "object": "chat.completion.chunk", "created": 0, "model": model}

        def emit(delta: dict, finish_reason=None) -> bytes:
            payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

        # 1-2. Reasoning split in two deltas
        yield emit({"role": "assistant", "reasoning_content": "User wants weather + time; "})
        await asyncio.sleep(0.005)
        yield emit({"reasoning_content": "calling both tools in parallel."})
        await asyncio.sleep(0.005)
        # 3-4. Content split in two deltas
        yield emit({"content": "Checking weather"})
        await asyncio.sleep(0.005)
        yield emit({"content": " and time for you."})
        await asyncio.sleep(0.005)

        # 5-7. tool_call[0] (get_weather): announce, then arguments in two pieces
        yield emit(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_weather",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ]
            }
        )
        await asyncio.sleep(0.005)
        yield emit({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Tokyo",'}}]})
        await asyncio.sleep(0.005)
        yield emit({"tool_calls": [{"index": 0, "function": {"arguments": '"unit":"celsius"}'}}]})
        await asyncio.sleep(0.005)

        # 8-9. tool_call[1] (get_time): announce + arguments in one piece
        yield emit(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "call_time",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": ""},
                    }
                ]
            }
        )
        await asyncio.sleep(0.005)
        yield emit({"tool_calls": [{"index": 1, "function": {"arguments": '{"city":"Tokyo"}'}}]})
        await asyncio.sleep(0.005)

        # 10. Finish
        yield emit({}, finish_reason="tool_calls")
        yield b"data: [DONE]\n\n"


@pytest.fixture
def mock_upstream() -> Iterator[MockUpstream]:
    with MockUpstream() as m:
        yield m


# ---------------------------------------------------------------------------
# Proxy app builder + request helper (module-level, generic)
# ---------------------------------------------------------------------------


def _build_proxy_app(*, mock_url: str | None = None, traj_file: Path | None = None, replay_cursor=None) -> FastAPI:
    config = ModelServiceConfig()
    # ReplayBackend never calls upstream, so mock_url is only relevant for forward mode.
    if mock_url is not None:
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


def _call_chat_completions(client: TestClient, *, stream: bool) -> dict:
    """One chat.completions call. Returns the assistant message dict.

    - non-stream: just unwraps ``choices[0].message``.
    - stream: replay always emits exactly one chunk + ``[DONE]`` (see
      ``completion_to_chunk_dict``), so the chunk's ``delta`` IS the full
      message — no aggregation needed.
    """
    payload = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
    if not stream:
        r = client.post("/v1/chat/completions", json=payload)
        assert r.status_code == 200
        return r.json()["choices"][0]["message"]

    with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True}) as r:
        assert r.status_code == 200
        body_bytes = b"".join(r.iter_bytes())
    chunks, _ = parse_sse_data_chunks(body_bytes)
    return chunks[0]["choices"][0]["delta"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProxyRecordReplay:
    """End-to-end: real TCP mock upstream <-> real proxy router + recorder/replayer."""

    def test_forward_non_stream(self, mock_upstream: MockUpstream, tmp_path):
        """Vendor field reaches the client; recorder writes a JSONL line with the full response."""
        traj_file = tmp_path / "traj.jsonl"
        proxy_app = _build_proxy_app(mock_url=mock_upstream.base_url, traj_file=traj_file)

        with TestClient(proxy_app) as client:
            r = client.post(
                "/v1/chat/completions",
                json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer test-key"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        mock_upstream.assert_message(body["choices"][0]["message"])

        rec = json.loads(traj_file.read_text(encoding="utf-8").strip())
        assert rec["status"] == "success"
        assert rec["stream"] is False
        assert rec["response"]["choices"][0]["finish_reason"] == "tool_calls"
        mock_upstream.assert_message(rec["response"]["choices"][0]["message"])

    def test_forward_stream(self, mock_upstream: MockUpstream, tmp_path):
        """Each upstream SSE chunk reaches the client; recorder gets the aggregated final completion
        with reasoning_content concatenated and tool_calls.arguments assembled from deltas."""
        traj_file = tmp_path / "traj.jsonl"
        proxy_app = _build_proxy_app(mock_url=mock_upstream.base_url, traj_file=traj_file)

        with TestClient(proxy_app) as client:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer test-key"},
            ) as r:
                body = b"".join(r.iter_bytes()).decode("utf-8")

        # Raw chunks make it to the client untouched
        assert '"reasoning_content": "User wants weather + time; "' in body
        assert '"reasoning_content": "calling both tools in parallel."' in body
        assert '"content": "Checking weather"' in body
        assert '"content": " and time for you."' in body
        assert '"name": "get_weather"' in body
        assert '"name": "get_time"' in body
        assert '"finish_reason": "tool_calls"' in body
        assert body.rstrip().endswith("data: [DONE]")

        # Recorder's aggregated message matches the canonical reply
        rec = json.loads(traj_file.read_text(encoding="utf-8").strip())
        assert rec["status"] == "success"
        assert rec["stream"] is True
        assert rec["response"]["choices"][0]["finish_reason"] == "tool_calls"
        mock_upstream.assert_message(rec["response"]["choices"][0]["message"])

    @pytest.mark.parametrize("replay_stream", [False, True], ids=["replay_nonstream", "replay_stream"])
    @pytest.mark.parametrize("record_stream", [False, True], ids=["record_nonstream", "record_stream"])
    def test_replay(self, mock_upstream: MockUpstream, tmp_path, record_stream: bool, replay_stream: bool):
        """Recorded mode and replayed mode are orthogonal — all 4 combinations of
        (stream/non-stream) on each side must yield the same full message."""
        traj_file = tmp_path / "traj.jsonl"

        # ---- record phase ----
        proxy_record = _build_proxy_app(mock_url=mock_upstream.base_url, traj_file=traj_file)
        with TestClient(proxy_record) as client:
            _call_chat_completions(client, stream=record_stream)

        # ---- replay phase: no upstream URL needed — ReplayBackend never calls upstream ----
        cursor = SequentialCursor.load(traj_file)
        proxy_replay = _build_proxy_app(replay_cursor=cursor)
        with TestClient(proxy_replay) as client:
            msg = _call_chat_completions(client, stream=replay_stream)

        mock_upstream.assert_message(msg)
