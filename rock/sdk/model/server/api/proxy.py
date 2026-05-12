"""OpenAI-compatible chat/completions proxy backed by the litellm SDK.

The proxy ``/v1/chat/completions`` handler routes a request to the configured
upstream LLM (or to the in-process traj-replay handler when ``replay_traj_path``
is set), forwards header/body, and applies retry via litellm's ``num_retries``.

Trajectory recording is wired up at startup in
``rock.sdk.model.server.main`` by registering ``TrajectoryRecorder`` as a
``litellm.callbacks`` entry — this handler does not carry a ``@record_traj``
decorator anymore.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import litellm
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.exceptions import APIError, AuthenticationError, BadRequestError, RateLimitError, Timeout

from rock.logger import init_logger
from rock.sdk.model.server.config import ModelServiceConfig

logger = init_logger(__name__)


proxy_router = APIRouter()


# Headers we never forward upstream:
#   - host / content-length / content-type: litellm rewrites the body and re-targets,
#     so the client's values would be wrong or misleading
#   - transfer-encoding / connection: true RFC 7230 hop-by-hop headers, scoped to
#     the client↔proxy connection only
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


@proxy_router.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    """OpenAI-compatible chat completions proxy endpoint.

    Routes via ``proxy_base_url`` / ``proxy_rules``, forwards Authorization-style
    headers, supports streaming, retries via litellm. In replay mode the request
    is dispatched to the registered ``traj-replay`` CustomLLM provider instead
    of being forwarded upstream.
    """
    config: ModelServiceConfig = request.app.state.model_service_config

    model_name = body.get("model", "")

    # 1. Route selection
    if config.replay_traj_path:
        litellm_model = f"traj-replay/{model_name or 'replay'}"
        api_base: str | None = None
        logger.info(f"[replay] dispatching '{model_name}' to traj-replay handler")
    else:
        api_base = get_base_url(model_name, config)
        # custom_openai is litellm's catch-all for OpenAI-compatible third-party endpoints
        # (DashScope, ModelScope, Groq, Mistral, ...). Unlike `openai/`, it does NOT do
        # model-name lookup, so arbitrary upstream model names like "glm-5" / "qwen-turbo"
        # work without "This model isn't mapped yet" errors.
        litellm_model = f"custom_openai/{model_name}" if model_name else "custom_openai/default"
        logger.info(f"Routing model '{model_name}' to {api_base}")

    # 2. Extract Bearer token (litellm needs api_key explicitly, not via headers)
    api_key = _extract_bearer_token(request.headers)

    # 3. Header forwarding (drop Authorization since we pass it via api_key, plus hop-by-hop)
    extra_headers = _filter_headers(request.headers)

    # 4. Build call kwargs (transparent passthrough of body fields)
    call_kwargs = dict(body)
    call_kwargs.pop("model", None)  # avoid duplicate kwargs
    is_stream = bool(call_kwargs.get("stream"))

    try:
        response = await litellm.acompletion(
            model=litellm_model,
            api_base=api_base,
            api_key=api_key,
            extra_headers=extra_headers,
            timeout=config.request_timeout,
            num_retries=config.num_retries,
            # Suppress litellm's "model isn't mapped yet" cost-calc exception for
            # arbitrary upstream models (glm-5, qwen-turbo, ...) that aren't in
            # litellm's pricing table. We don't care about cost tracking here, so
            # zero rates make the calc succeed cleanly with response_cost=0.
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

    # 4. Streaming vs non-streaming response
    if is_stream:
        return StreamingResponse(_sse_iter(response), media_type="text/event-stream")

    # litellm returns a ModelResponse pydantic; expose the OpenAI-shape dict.
    if hasattr(response, "model_dump"):
        body_out = response.model_dump()
    else:
        body_out = response  # already a dict (replay path can short-circuit)
    return JSONResponse(status_code=200, content=body_out)
