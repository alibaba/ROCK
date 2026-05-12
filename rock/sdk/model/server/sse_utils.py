"""SSE codec utilities for the chat/completions proxy.

Three pure helpers, no openai/litellm dependencies:

- :func:`parse_sse_data_chunks` — incremental SSE byte stream → list of decoded
  ``data:`` payload dicts (used by the forward path to feed chunks into the
  stream-state aggregator while bytes pass through verbatim to the client).
- :func:`completion_to_chunk_dict` — convert a non-streaming ``chat.completion``
  response into a single ``chat.completion.chunk`` dict, by renaming
  ``message`` → ``delta``. Used by the replay path's streaming output.
- :func:`encode_sse_event` — encode a payload dict as ``data: <json>\\n\\n``
  bytes (one SSE event).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Final

# Terminal SSE event sent at the end of a chat/completions stream.
SSE_DONE: Final[bytes] = b"data: [DONE]\n\n"


def parse_sse_data_chunks(buffer: bytes) -> tuple[list[dict], bytes]:
    """Extract complete SSE events from a (possibly partial) byte buffer.

    Returns ``(chunks, leftover)``: the parsed ``data:`` JSON payload dicts and
    the bytes that did not yet form a complete event (``\\n\\n``-terminated).

    - ``data: [DONE]`` is skipped (terminal marker, has no JSON payload).
    - Lines that don't start with ``data:`` (``event:`` / ``id:`` / blank)
      are ignored.
    - Malformed JSON in a ``data:`` line is silently skipped — caller logs at
      its own discretion (typically ``debug``).

    Caller pattern::

        chunks, buffer = parse_sse_data_chunks(buffer + new_bytes)
        for chunk_dict in chunks:
            ... feed to aggregator, etc ...
    """
    chunks: list[dict] = []
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
                chunks.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
    return chunks, buffer


def completion_to_chunk_dict(response: dict, *, model: str) -> dict:
    """Convert a recorded ``chat.completion`` dict into a single
    ``chat.completion.chunk`` dict, suitable for re-streaming.

    Only ``message`` → ``delta`` is renamed; every other field (including
    provider-specific extras like ``reasoning_content`` inside the message)
    flows through unchanged. ``id`` / ``created`` are synthesized when missing.

    ``tool_calls`` items get a positional ``index`` injected if missing — the
    OpenAI streaming spec requires it on chunk deltas (a recorded non-stream
    ``message.tool_calls`` carries no ``index``, but downstream stream parsers
    e.g. the openai SDK will reject the chunk without one).
    """
    choices_in = response.get("choices") or []
    choices_out = []
    for choice in choices_in:
        delta = dict(choice.get("message") or {})
        if "tool_calls" in delta and delta["tool_calls"]:
            delta["tool_calls"] = [{"index": tc.get("index", i), **tc} for i, tc in enumerate(delta["tool_calls"])]
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


def encode_sse_event(data: dict) -> bytes:
    """Encode a JSON payload as one SSE ``data:`` event (terminated by ``\\n\\n``)."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
