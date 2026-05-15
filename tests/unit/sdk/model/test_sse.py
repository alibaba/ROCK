"""Tests for the pure SSE codec utilities (no openai/litellm dependencies)."""

import json

from rock.sdk.model.server.sse import (
    SSE_DONE,
    completion_to_chunk_dict,
    encode_sse_event,
    parse_sse_data_chunks,
)

# ---------- parse_sse_data_chunks ----------


def test_parse_returns_complete_events_and_leftover_buffer():
    raw = b'data: {"a": 1}\n\ndata: {"a": 2}\n\ndata: {"a": 3}'  # 3rd event is incomplete
    chunks, leftover = parse_sse_data_chunks(raw)

    assert chunks == [{"a": 1}, {"a": 2}]
    assert leftover == b'data: {"a": 3}'


def test_parse_skips_done_marker():
    raw = b'data: {"x": 1}\n\ndata: [DONE]\n\n'
    chunks, leftover = parse_sse_data_chunks(raw)

    assert chunks == [{"x": 1}]
    assert leftover == b""


def test_parse_skips_non_data_lines():
    raw = b'event: progress\ndata: {"y": 2}\nid: abc\n\n'
    chunks, leftover = parse_sse_data_chunks(raw)

    assert chunks == [{"y": 2}]
    assert leftover == b""


def test_parse_silently_skips_malformed_json():
    raw = b'data: not-json-at-all\n\ndata: {"ok": true}\n\n'
    chunks, leftover = parse_sse_data_chunks(raw)

    assert chunks == [{"ok": True}]
    assert leftover == b""


def test_parse_handles_empty_buffer():
    chunks, leftover = parse_sse_data_chunks(b"")
    assert chunks == []
    assert leftover == b""


def test_parse_incremental_streaming_pattern():
    """Simulates feeding bytes in arbitrary chunks; final concatenation == all events."""
    full_stream = b'data: {"i": 0}\n\ndata: {"i": 1}\n\ndata: {"i": 2}\n\ndata: [DONE]\n\n'
    fragments = [full_stream[i : i + 5] for i in range(0, len(full_stream), 5)]

    buffer = b""
    collected: list[dict] = []
    for frag in fragments:
        new_chunks, buffer = parse_sse_data_chunks(buffer + frag)
        collected.extend(new_chunks)

    assert collected == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert buffer == b""


def test_parse_handles_unicode_payload():
    raw = b'data: {"content": "\xe4\xbd\xa0\xe5\xa5\xbd"}\n\n'  # "你好" UTF-8
    chunks, _ = parse_sse_data_chunks(raw)
    assert chunks == [{"content": "你好"}]


# ---------- completion_to_chunk_dict ----------


def test_completion_to_chunk_renames_message_to_delta():
    response = {
        "id": "rec-1",
        "object": "chat.completion",
        "created": 100,
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
    }
    chunk = completion_to_chunk_dict(response, model="gpt-4")

    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["id"] == "rec-1"
    assert chunk["created"] == 100
    assert chunk["model"] == "gpt-4"
    assert chunk["choices"][0]["delta"] == {"role": "assistant", "content": "hi"}
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert chunk["choices"][0]["index"] == 0
    assert "message" not in chunk["choices"][0]


def test_completion_to_chunk_preserves_provider_specific_message_fields():
    """reasoning_content kept verbatim; tool_calls get a positional index injected
    (required by the OpenAI streaming spec — see test below)."""
    response = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "step-by-step thinking",
                    "tool_calls": [{"id": "t1", "type": "function"}],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    chunk = completion_to_chunk_dict(response, model="glm-5")

    assert chunk["choices"][0]["delta"]["reasoning_content"] == "step-by-step thinking"
    assert chunk["choices"][0]["delta"]["tool_calls"] == [{"index": 0, "id": "t1", "type": "function"}]
    assert chunk["choices"][0]["finish_reason"] == "tool_calls"


def test_completion_to_chunk_injects_tool_call_index_for_openai_sdk_compat():
    """A recorded non-stream message has tool_calls without 'index'; the OpenAI
    streaming spec requires it on chunk deltas, and the openai SDK's
    ChatCompletionChunk.model_validate() rejects the chunk otherwise. We inject
    a positional index so replay-stream output is parseable by strict clients."""
    response = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                        {"id": "b", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    chunk = completion_to_chunk_dict(response, model="m")
    tcs = chunk["choices"][0]["delta"]["tool_calls"]
    assert [tc["index"] for tc in tcs] == [0, 1]

    # End-to-end: openai SDK accepts the chunk
    from openai.types.chat import ChatCompletionChunk

    ChatCompletionChunk.model_validate(chunk)  # must not raise


def test_completion_to_chunk_preserves_explicit_tool_call_index():
    """If the recorded tool_calls already have 'index', we don't overwrite it."""
    response = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"index": 5, "id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    chunk = completion_to_chunk_dict(response, model="m")
    assert chunk["choices"][0]["delta"]["tool_calls"][0]["index"] == 5


def test_completion_to_chunk_synthesizes_id_and_created_when_missing():
    chunk = completion_to_chunk_dict(
        {"choices": [{"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}]},
        model="any",
    )
    assert chunk["id"].startswith("chatcmpl-")
    assert isinstance(chunk["created"], int) and chunk["created"] > 0
    assert chunk["model"] == "any"


def test_completion_to_chunk_handles_empty_choices():
    chunk = completion_to_chunk_dict({"choices": []}, model="m")
    assert chunk["choices"] == []


# ---------- encode_sse_event ----------


def test_encode_sse_event_appends_double_newline_terminator():
    out = encode_sse_event({"k": "v"})
    assert out.endswith(b"\n\n")
    assert out.startswith(b"data: ")
    body = out[len(b"data: ") : -len(b"\n\n")]
    assert json.loads(body) == {"k": "v"}


def test_encode_sse_event_preserves_unicode_without_escapes():
    out = encode_sse_event({"content": "你好"})
    # ensure_ascii=False is critical so Chinese stays readable in the wire format
    assert "你好".encode() in out


def test_sse_done_constant():
    assert SSE_DONE == b"data: [DONE]\n\n"


# ---------- round-trip ----------


def test_roundtrip_encode_then_parse():
    """encode → parse must round-trip a payload dict."""
    payloads = [{"i": 0, "text": "alpha"}, {"i": 1, "text": "beta 中文"}]
    wire = b"".join(encode_sse_event(p) for p in payloads) + SSE_DONE
    chunks, leftover = parse_sse_data_chunks(wire)

    assert chunks == payloads
    assert leftover == b""
