"""OpenAI-compatible chat/completions proxy with trajectory record/replay.

Two paths share this handler:

1. **Forward / record mode** (default) — body bytes are POSTed verbatim to the
   configured upstream via plain ``httpx``. The upstream response is forwarded
   byte-for-byte back to the client (raw JSON for non-stream, raw SSE bytes
   for stream). On the side we run a parser (``ChatCompletionChunk`` +
   ``ChatCompletionStreamState`` from the openai SDK) to aggregate streaming
   chunks into a final ChatCompletion that the recorder writes to JSONL. The
   forward path itself does NOT depend on OpenAI types — anything the upstream
   returns (provider-specific ``reasoning_content``, ``citations``, ...) is
   passed through untouched.

2. **Replay mode** (``replay_traj_path`` set) — the request is served directly
   from the next record in ``app.state.replay_cursor`` without any upstream
   call. Streaming emits the recorded response as one SSE chunk + ``[DONE]``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletionChunk

from rock.logger import init_logger
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.integrations.traj_recorder import TrajectoryRecorder
from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor, TrajectoryExhausted

logger = init_logger(__name__)


proxy_router = APIRouter()


# Headers we never forward upstream:
#   - host / content-length: rebuilt by httpx for the upstream request
#   - transfer-encoding / connection: RFC 7230 hop-by-hop, scoped to one connection
_HEADERS_NOT_TO_FORWARD = frozenset({"host", "content-length", "transfer-encoding", "connection"})


def get_base_url(model_name: str, config: ModelServiceConfig) -> str:
    """Pick the upstream base URL by model name.

    ``proxy_base_url`` takes precedence; falls back to ``proxy_rules[model]`` and
    then ``proxy_rules["default"]``. Trailing slashes are stripped so the caller
    can append ``/chat/completions`` directly.
    """
    if config.proxy_base_url:
        return config.proxy_base_url.rstrip("/")

    if not model_name:
        raise HTTPException(status_code=400, detail="Model name is required for routing.")

    rules = config.proxy_rules
    base_url = rules.get(model_name) or rules.get("default")
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model_name}' is not configured and no 'default' rule found.",
        )

    return base_url.rstrip("/")


def _filter_headers(headers) -> dict[str, str]:
    """Drop headers that are scoped to the client↔proxy hop or rebuilt by httpx.
    ``Authorization`` is forwarded verbatim — proxy stays stateless about which
    API key the client uses."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HEADERS_NOT_TO_FORWARD:
            continue
        out[key] = value
    return out


def _completion_to_chunk(response: dict, *, model: str) -> dict:
    """Convert a recorded ``chat.completion`` response into a single
    ``chat.completion.chunk`` shape (move ``message`` → ``delta``). Used only by
    the replay streaming path."""
    choices_in = response.get("choices") or []
    choices_out = []
    for choice in choices_in:
        delta = dict(choice.get("message") or {})
        choices_out.append(
            {
                "index": choice.get("index", 0),
                "delta": delta,
                "finish_reason": choice.get("finish_reason"),
                "logprobs": choice.get("logprobs"),
            }
        )
    return {
        "id": response.get("id") or f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "created": response.get("created") or int(time.time()),
        "model": response.get("model") or model,
        "choices": choices_out,
    }


async def _replay_sse_iter(response: dict, *, model: str) -> AsyncIterator[bytes]:
    """Emit a recorded response as one SSE chunk + ``[DONE]``."""
    chunk = _completion_to_chunk(response, model=model)
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _parse_sse_chunks_into_state(buffer: bytes, state: ChatCompletionStreamState) -> bytes:
    """Pull complete SSE events out of ``buffer`` and feed each ``data:`` line
    (other than ``[DONE]``) to the openai stream-state aggregator. Returns the
    leftover bytes that did not yet form a complete event."""
    while b"\n\n" in buffer:
        event, buffer = buffer.split(b"\n\n", 1)
        for raw_line in event.split(b"\n"):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                state.handle_chunk(ChatCompletionChunk.model_validate(json.loads(payload)))
            except Exception as exc:  # parser error: forward continues, traj will be partial
                logger.debug(f"[record] chunk parse failed (forward continues): {exc}")
    return buffer


async def _forward_stream_and_record(
    *,
    upstream_url: str,
    body_bytes: bytes,
    fwd_headers: dict[str, str],
    timeout: float,
    request_dict: dict[str, Any],
    recorder: TrajectoryRecorder | None,
) -> AsyncIterator[bytes]:
    """SSE bytes are forwarded verbatim; chunks are parsed in parallel and
    aggregated into the final ChatCompletion that the recorder writes to JSONL."""
    state = ChatCompletionStreamState()
    start = time.time()
    parse_buffer = b""
    upstream_status = 0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", upstream_url, content=body_bytes, headers=fwd_headers) as r:
                upstream_status = r.status_code
                async for chunk in r.aiter_bytes():
                    yield chunk
                    parse_buffer = _parse_sse_chunks_into_state(parse_buffer + chunk, state)
    except httpx.RequestError as exc:
        # Connection died mid-stream. The bytes already sent reach the client;
        # we still try to record what we got.
        if recorder is not None:
            await recorder.record(
                request=request_dict,
                response=None,
                status="failure",
                start_time=start,
                end_time=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            )
        return

    if recorder is None:
        return

    status = "success" if upstream_status < 400 else "failure"
    final_dict: dict | None = None
    if status == "success":
        try:
            final_dict = state.get_final_completion().model_dump()
        except Exception as exc:
            logger.warning(f"[record] stream aggregation failed: {exc}")

    await recorder.record(
        request=request_dict,
        response=final_dict,
        status=status,
        start_time=start,
        end_time=time.time(),
        error=None if status == "success" else f"upstream_status={upstream_status}",
    )


@proxy_router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions proxy endpoint.

    Reads the body as raw bytes (no parsing on the forward path) and either
    serves it from the replay cursor or forwards it to the configured upstream.
    """
    config: ModelServiceConfig = request.app.state.model_service_config
    recorder: TrajectoryRecorder | None = getattr(request.app.state, "recorder", None)

    body_bytes = await request.body()
    try:
        request_dict = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Request body is not valid JSON.")
    if not isinstance(request_dict, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    model_name = request_dict.get("model", "")
    is_stream = bool(request_dict.get("stream"))

    # ---- Replay mode: short-circuit, no upstream call ----
    if config.replay_traj_path:
        cursor: SequentialCursor = request.app.state.replay_cursor
        try:
            record = await cursor.next(expected_model=model_name)
        except TrajectoryExhausted as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        response_dict = record.get("response")
        if not isinstance(response_dict, dict):
            raise HTTPException(
                status_code=500,
                detail=f"replay record at step {cursor.position - 1} has no usable response dict",
            )
        logger.info(f"[replay] step {cursor.position}/{cursor.total} served for model={model_name!r}")

        if is_stream:
            return StreamingResponse(
                _replay_sse_iter(response_dict, model=model_name),
                media_type="text/event-stream",
            )
        return JSONResponse(status_code=200, content=response_dict)

    # ---- Forward / record mode: byte-passthrough via httpx ----
    upstream_url = f"{get_base_url(model_name, config)}/chat/completions"
    fwd_headers = _filter_headers(request.headers)
    logger.info(f"Routing model {model_name!r} to {upstream_url}")

    if is_stream:
        return StreamingResponse(
            _forward_stream_and_record(
                upstream_url=upstream_url,
                body_bytes=body_bytes,
                fwd_headers=fwd_headers,
                timeout=config.request_timeout,
                request_dict=request_dict,
                recorder=recorder,
            ),
            media_type="text/event-stream",
        )

    # Non-stream: single POST, return upstream's status + body verbatim, record on the side.
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=config.request_timeout) as client:
            r = await client.post(upstream_url, content=body_bytes, headers=fwd_headers)
    except httpx.TimeoutException as exc:
        if recorder is not None:
            await recorder.record(
                request=request_dict,
                response=None,
                status="failure",
                start_time=start,
                end_time=time.time(),
                error=f"timeout: {exc}",
            )
        raise HTTPException(status_code=504, detail=f"Upstream timed out: {exc}")
    except httpx.RequestError as exc:
        if recorder is not None:
            await recorder.record(
                request=request_dict,
                response=None,
                status="failure",
                start_time=start,
                end_time=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            )
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

    response_text = r.text  # bytes already read by httpx; .text decodes once
    response_dict: dict | None = None
    try:
        parsed = json.loads(response_text) if response_text else None
        if isinstance(parsed, dict):
            response_dict = parsed
    except json.JSONDecodeError:
        pass

    if recorder is not None:
        await recorder.record(
            request=request_dict,
            response=response_dict,
            status="success" if r.status_code < 400 else "failure",
            start_time=start,
            end_time=time.time(),
            error=None if r.status_code < 400 else f"upstream_status={r.status_code}",
        )

    # Forward bytes verbatim — preserves any provider-specific fields untouched.
    media_type = r.headers.get("content-type", "application/json")
    return Response(content=response_text, status_code=r.status_code, media_type=media_type)
