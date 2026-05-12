"""OpenAI-compatible chat/completions proxy with trajectory record/replay.

Two backends share the ``/v1/chat/completions`` route:

1. **ForwardBackend** (default) — body bytes are POSTed verbatim to the
   configured upstream via plain ``httpx``. The upstream response is forwarded
   byte-for-byte back to the client (raw JSON for non-stream, raw SSE bytes
   for stream). On the side we run a parser (``ChatCompletionChunk`` +
   ``ChatCompletionStreamState`` from the openai SDK) to aggregate streaming
   chunks into a final ChatCompletion that the recorder writes to JSONL. The
   forward path itself does NOT depend on OpenAI types — anything the upstream
   returns (provider-specific ``reasoning_content``, ``citations``, ...) is
   passed through untouched.

2. **ReplayBackend** (``replay_file`` set) — the request is served
   directly from the next record in the ``SequentialCursor`` without any
   upstream call. Streaming emits the recorded response as one SSE chunk +
   ``[DONE]``.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from rock.logger import init_logger
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.sse import (
    SSE_DONE,
    completion_to_chunk_dict,
    encode_sse_event,
    parse_sse_data_chunks,
)
from rock.sdk.model.server.traj import SequentialCursor, TrajectoryExhausted, TrajectoryRecorder

logger = init_logger(__name__)


proxy_router = APIRouter()


# Headers we never forward upstream:
#   - host / content-length: rebuilt by httpx for the upstream request
#   - transfer-encoding / connection: RFC 7230 hop-by-hop, scoped to one connection
_HEADERS_NOT_TO_FORWARD = frozenset({"host", "content-length", "transfer-encoding", "connection"})

# Retry knobs for upstream POST. Read at call-time so tests can monkeypatch them.
# Default: up to 6 attempts with exponential backoff (2s → 4s → 8s → 16s → 32s, jittered).
_RETRY_MAX_ATTEMPTS = 6
_RETRY_DELAY_SECONDS = 2.0
_RETRY_BACKOFF = 2.0


async def _send_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    body_bytes: bytes,
    headers: dict[str, str],
    retryable_codes: list[int],
) -> httpx.Response:
    """POST with retry on connection errors and whitelisted statuses, returning
    an open streaming response.

    Always uses ``stream=True`` so the same path serves both stream and non-stream
    callers — non-stream just calls ``await resp.aread()`` to materialize the body.
    Assumes a failed upstream returns its error body before any byte is yielded
    to downstream (so retry can still discard it cleanly).

    Caller MUST ``await resp.aclose()`` after consuming.
    """
    last_exc: Exception | None = None
    delay = _RETRY_DELAY_SECONDS
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = await client.send(
                client.build_request("POST", url, content=body_bytes, headers=headers),
                stream=True,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt >= _RETRY_MAX_ATTEMPTS:
                raise
            logger.warning(f"connect failed (attempt {attempt}/{_RETRY_MAX_ATTEMPTS}): {exc}")
            await asyncio.sleep(random.uniform(0, delay * 2))
            delay *= _RETRY_BACKOFF
            continue

        if resp.status_code in retryable_codes and attempt < _RETRY_MAX_ATTEMPTS:
            await resp.aclose()
            logger.warning(f"upstream status {resp.status_code}, retry {attempt}/{_RETRY_MAX_ATTEMPTS}")
            await asyncio.sleep(random.uniform(0, delay * 2))
            delay *= _RETRY_BACKOFF
            continue

        return resp

    raise last_exc  # pragma: no cover  # unreachable


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


class ReplayBackend:
    """Serves requests from a pre-recorded trajectory; no upstream calls made."""

    def __init__(self, cursor: SequentialCursor) -> None:
        self._cursor = cursor

    async def serve(self, *, model_name: str, is_stream: bool, **_: Any) -> Response:
        try:
            record = await self._cursor.next(expected_model=model_name)
        except TrajectoryExhausted as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        response_dict = record.get("response")
        if not isinstance(response_dict, dict):
            raise HTTPException(
                status_code=500,
                detail=f"replay record at step {self._cursor.position - 1} has no usable response dict",
            )
        logger.info(f"[replay] step {self._cursor.position}/{self._cursor.total} served for model={model_name!r}")

        if is_stream:
            return StreamingResponse(
                self._sse_iter(response_dict, model=model_name),
                media_type="text/event-stream",
            )
        return JSONResponse(status_code=200, content=response_dict)

    @staticmethod
    async def _sse_iter(response: dict, *, model: str) -> AsyncIterator[bytes]:
        """Emit a recorded response as one SSE chunk + ``[DONE]``."""
        yield encode_sse_event(completion_to_chunk_dict(response, model=model))
        yield SSE_DONE


class ForwardBackend:
    """Forwards requests byte-for-byte to the upstream and optionally records the trajectory."""

    def __init__(self, config: ModelServiceConfig, recorder: TrajectoryRecorder | None = None) -> None:
        self._config = config
        self._recorder = recorder

    def _resolve_base_url(self, model_name: str) -> str:
        """Pick the upstream base URL by model name.

        ``proxy_base_url`` takes precedence; falls back to ``proxy_rules[model]`` and
        then ``proxy_rules["default"]``. Trailing slashes are stripped so the caller
        can append ``/chat/completions`` directly.
        """
        if self._config.proxy_base_url:
            return self._config.proxy_base_url.rstrip("/")

        if not model_name:
            raise HTTPException(status_code=400, detail="Model name is required for routing.")

        rules = self._config.proxy_rules
        base_url = rules.get(model_name) or rules.get("default")
        if not base_url:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model_name}' is not configured and no 'default' rule found.",
            )

        return base_url.rstrip("/")

    async def serve(
        self,
        *,
        model_name: str,
        is_stream: bool,
        body_bytes: bytes,
        fwd_headers: dict[str, str],
        request_dict: dict[str, Any],
        **_: Any,
    ) -> Response:
        upstream_url = f"{self._resolve_base_url(model_name)}/chat/completions"
        logger.info(f"Routing model {model_name!r} to {upstream_url}")

        if is_stream:
            return StreamingResponse(
                self._stream_and_record(
                    upstream_url=upstream_url,
                    body_bytes=body_bytes,
                    fwd_headers=fwd_headers,
                    request_dict=request_dict,
                ),
                media_type="text/event-stream",
            )

        # Non-stream: same retry path as stream (open with stream=True), then aread() the body.
        start = time.time()
        async with httpx.AsyncClient(timeout=self._config.request_timeout) as client:
            try:
                resp = await _send_with_retry(
                    client,
                    upstream_url,
                    body_bytes=body_bytes,
                    headers=fwd_headers,
                    retryable_codes=self._config.retryable_status_codes,
                )
            except httpx.TimeoutException as exc:
                if self._recorder is not None:
                    await self._recorder.record(
                        request=request_dict,
                        response=None,
                        status="failure",
                        start_time=start,
                        end_time=time.time(),
                        error=f"timeout: {exc}",
                    )
                raise HTTPException(status_code=504, detail=f"Upstream timed out: {exc}")
            except httpx.RequestError as exc:
                if self._recorder is not None:
                    await self._recorder.record(
                        request=request_dict,
                        response=None,
                        status="failure",
                        start_time=start,
                        end_time=time.time(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

            try:
                response_bytes = await resp.aread()
                status_code = resp.status_code
                content_type = resp.headers.get("content-type", "application/json")
            finally:
                await resp.aclose()

        response_text = response_bytes.decode("utf-8", errors="replace")
        response_dict: dict | None = None
        try:
            parsed = json.loads(response_text) if response_text else None
            if isinstance(parsed, dict):
                response_dict = parsed
        except json.JSONDecodeError:
            pass

        if self._recorder is not None:
            await self._recorder.record(
                request=request_dict,
                response=response_dict,
                status="success" if status_code < 400 else "failure",
                start_time=start,
                end_time=time.time(),
                error=None if status_code < 400 else f"upstream_status={status_code}",
            )

        # Forward bytes verbatim — preserves any provider-specific fields untouched.
        return Response(content=response_bytes, status_code=status_code, media_type=content_type)

    async def _stream_and_record(
        self,
        *,
        upstream_url: str,
        body_bytes: bytes,
        fwd_headers: dict[str, str],
        request_dict: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """SSE bytes are forwarded verbatim; chunks are parsed in parallel and
        aggregated into the final ChatCompletion that the recorder writes to JSONL.

        Retry on connection errors and whitelisted statuses happens BEFORE any byte
        is yielded; mid-stream connection drops are not retried (would corrupt the
        client transmission)."""
        # openai SDK is used purely as a stream-aggregation parser — keep the import
        # local so module load doesn't pull it in for callers that never stream.
        from openai.lib.streaming.chat import ChatCompletionStreamState
        from openai.types.chat import ChatCompletionChunk

        state = ChatCompletionStreamState()
        start = time.time()
        parse_buffer = b""
        upstream_status = 0

        async with httpx.AsyncClient(timeout=self._config.request_timeout) as client:
            try:
                resp = await _send_with_retry(
                    client,
                    upstream_url,
                    body_bytes=body_bytes,
                    headers=fwd_headers,
                    retryable_codes=self._config.retryable_status_codes,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if self._recorder is not None:
                    await self._recorder.record(
                        request=request_dict,
                        response=None,
                        status="failure",
                        start_time=start,
                        end_time=time.time(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return

            try:
                upstream_status = resp.status_code
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    chunk_dicts, parse_buffer = parse_sse_data_chunks(parse_buffer + chunk)
                    for chunk_dict in chunk_dicts:
                        try:
                            state.handle_chunk(ChatCompletionChunk.model_validate(chunk_dict))
                        except Exception as exc:  # parser error: forward continues, traj will be partial
                            logger.debug(f"[record] chunk parse failed (forward continues): {exc}")
            except httpx.RequestError as exc:
                # Connection died mid-stream — bytes already sent reach the client;
                # record what we got and return.
                if self._recorder is not None:
                    await self._recorder.record(
                        request=request_dict,
                        response=None,
                        status="failure",
                        start_time=start,
                        end_time=time.time(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return
            finally:
                await resp.aclose()

        if self._recorder is None:
            return

        status = "success" if upstream_status < 400 else "failure"
        final_dict: dict | None = None
        if status == "success":
            try:
                final_dict = state.get_final_completion().model_dump()
            except Exception as exc:
                logger.warning(f"[record] stream aggregation failed: {exc}")

        await self._recorder.record(
            request=request_dict,
            response=final_dict,
            status=status,
            start_time=start,
            end_time=time.time(),
            error=None if status == "success" else f"upstream_status={upstream_status}",
        )


CompletionBackend = ReplayBackend | ForwardBackend


def _get_backend(request: Request) -> CompletionBackend:
    """Typed accessor for the backend attached at startup by ``_configure_proxy_integrations``."""
    return request.app.state.backend


@proxy_router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions proxy endpoint.

    Reads the body as raw bytes (no parsing on the forward path) and delegates
    to the backend attached at startup (replay or forward).
    """
    body_bytes = await request.body()
    try:
        request_dict = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Request body is not valid JSON.")
    if not isinstance(request_dict, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    model_name = request_dict.get("model", "")
    is_stream = bool(request_dict.get("stream"))
    fwd_headers = _filter_headers(request.headers)

    backend = _get_backend(request)
    return await backend.serve(
        model_name=model_name,
        is_stream=is_stream,
        body_bytes=body_bytes,
        fwd_headers=fwd_headers,
        request_dict=request_dict,
    )
