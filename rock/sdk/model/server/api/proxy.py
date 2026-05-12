"""OpenAI-compatible chat/completions proxy.

Two paths share this handler:

1. **Record / forward mode** (default) — ``litellm.acompletion`` is called with
   the user-supplied model/messages, the upstream is selected from
   ``proxy_base_url`` / ``proxy_rules``, retries come from litellm's
   ``num_retries``, and the recorded JSONL trajectory is written by a
   ``litellm.callbacks`` entry registered at startup (see
   ``rock.sdk.model.server.main``).

2. **Replay mode** (``replay_traj_path`` set) — the request is served directly
   from the next record in ``app.state.replay_cursor`` without going through
   litellm at all. We have a complete OpenAI-shape response on disk, so there's
   no value in routing through CustomLLM/CustomStreamWrapper just to translate
   formats. Streaming emits the recorded response as a single SSE chunk +
   ``[DONE]``, mirroring litellm's own ``MockResponseIterator`` strategy.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import litellm
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.exceptions import APIError, AuthenticationError, BadRequestError, RateLimitError, Timeout

from rock.logger import init_logger
from rock.sdk.model.server.config import ModelServiceConfig
from rock.sdk.model.server.integrations.traj_replayer import SequentialCursor, TrajectoryExhausted

logger = init_logger(__name__)


proxy_router = APIRouter()


# Headers we never forward upstream:
#   - host / content-length / content-type: litellm rewrites the body and re-targets,
#     so the client's values would be wrong or misleading
#   - transfer-encoding / connection: true RFC 7230 hop-by-hop headers, scoped to
#     the client↔proxy connection only
#   - authorization: extracted into api_key kwarg, see _extract_bearer_token
_HEADERS_NOT_TO_FORWARD = frozenset(
    {"host", "content-length", "content-type", "transfer-encoding", "connection", "authorization"}
)


def _extract_bearer_token(headers) -> str | None:
    """Pull the Bearer token out of the Authorization header.

    litellm's OpenAI client needs the API key as an explicit ``api_key=`` kwarg —
    setting Authorization in extra_headers does not work because litellm always
    regenerates that header from ``api_key`` (or env vars). So we extract it here
    and let the proxy stay stateless about which key the client is using.
    """
    auth = headers.get("authorization") or headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return auth.strip()


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
    forwarded = {}
    for key, value in headers.items():
        if key.lower() in _HEADERS_NOT_TO_FORWARD:
            continue
        forwarded[key] = value
    return forwarded


def _format_error_response(exc: Exception) -> JSONResponse:
    """Render a litellm exception as the legacy ``{error:{message,type,code}}`` JSON.

    Agent-side logic keys off message substrings (e.g. "context length exceeded",
    "content violation"), so we keep the message verbatim from the upstream.
    """
    status_code = getattr(exc, "status_code", None) or 502
    message = str(exc)
    error_type = type(exc).__name__
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": f"LLM backend error: {message}",
                "type": error_type,
                "code": status_code,
            }
        },
    )


async def _sse_iter(stream: AsyncIterator[Any]) -> AsyncIterator[bytes]:
    """Convert a litellm async chunk stream into Server-Sent Events bytes."""
    try:
        async for chunk in stream:
            payload = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    finally:
        yield b"data: [DONE]\n\n"


def _completion_to_chunk(response: dict, *, model: str) -> dict:
    """Convert a recorded ``chat.completion`` response into a single
    ``chat.completion.chunk`` shape (move ``message`` → ``delta``).

    Mirrors what litellm's ``convert_model_response_to_streaming`` does for its
    own non-streaming providers — preserves ``finish_reason``, ``tool_calls``
    and any other fields verbatim by simply renaming the wrapper key.
    """
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
    """Emit a recorded response as a single SSE chunk + ``[DONE]``.

    The whole recorded answer goes out in one chunk — same strategy as
    litellm's ``MockResponseIterator``. Most agents accumulate SSE into a
    final string anyway; faking finer-grained streaming would just add code
    without buying anyone anything.
    """
    chunk = _completion_to_chunk(response, model=model)
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@proxy_router.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    """OpenAI-compatible chat completions proxy endpoint.

    In replay mode (``replay_traj_path`` set), serves the next record from
    ``app.state.replay_cursor`` directly — no litellm involvement. Otherwise
    forwards to the configured upstream via ``litellm.acompletion``.
    """
    config: ModelServiceConfig = request.app.state.model_service_config
    model_name = body.get("model", "")
    is_stream = bool(body.get("stream"))

    # ---- Replay mode: short-circuit, never touch litellm ----
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

    # ---- Forward / record mode: go through litellm ----
    api_base = get_base_url(model_name, config)
    # custom_openai is litellm's catch-all for OpenAI-compatible third-party endpoints
    # (DashScope, ModelScope, Groq, Mistral, ...). Unlike `openai/`, it does NOT do
    # model-name lookup, so arbitrary upstream model names like "glm-5" / "qwen-turbo"
    # work without "This model isn't mapped yet" errors.
    litellm_model = f"custom_openai/{model_name}" if model_name else "custom_openai/default"
    logger.info(f"Routing model '{model_name}' to {api_base}")

    api_key = _extract_bearer_token(request.headers)
    extra_headers = _filter_headers(request.headers)

    call_kwargs = dict(body)
    call_kwargs.pop("model", None)

    try:
        response = await litellm.acompletion(
            model=litellm_model,
            api_base=api_base,
            api_key=api_key,
            extra_headers=extra_headers,
            timeout=config.request_timeout,
            num_retries=config.num_retries,
            # Zero-cost rates suppress "model isn't mapped yet" from litellm's
            # post-call cost calculator for arbitrary upstream model names.
            input_cost_per_token=0,
            output_cost_per_token=0,
            **call_kwargs,
        )
    except (RateLimitError, APIError, BadRequestError, AuthenticationError, Timeout) as exc:
        logger.warning(f"litellm error for model '{model_name}': {exc}")
        return _format_error_response(exc)
    except Exception as exc:  # pragma: no cover - last-resort safety net
        logger.error(f"Unexpected proxy error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if is_stream:
        return StreamingResponse(_sse_iter(response), media_type="text/event-stream")

    body_out = response.model_dump() if hasattr(response, "model_dump") else response
    return JSONResponse(status_code=200, content=body_out)
