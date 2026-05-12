"""Tests for SequentialCursor + TrajectoryReplayer."""

import json
from types import SimpleNamespace

import pytest
from litellm.llms.custom_llm import CustomLLMError

from rock.sdk.model.server.integrations.traj_replayer import (
    SequentialCursor,
    TrajectoryReplayer,
)


def _record(*, msg: str, model: str = "gpt-3.5-turbo", call_id: str = "x") -> dict:
    """Build a minimal StandardLoggingPayload-shaped record."""
    return {
        "id": call_id,
        "model": model,
        "messages": [{"role": "user", "content": msg}],
        "response": {
            "id": call_id,
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"reply: {msg}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    }


def _write_jsonl(path, records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ----- SequentialCursor -----


def test_cursor_load_from_single_file(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a"), _record(msg="b")])

    cur = SequentialCursor.load(p)
    assert cur.total == 2
    assert cur.position == 0


def test_cursor_load_skips_empty_lines(tmp_path):
    p = tmp_path / "traj.jsonl"
    p.write_text(
        json.dumps(_record(msg="a")) + "\n\n  \n" + json.dumps(_record(msg="b")) + "\n",
        encoding="utf-8",
    )

    cur = SequentialCursor.load(p)
    assert cur.total == 2


def test_cursor_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        SequentialCursor.load(tmp_path / "missing.jsonl")


def test_cursor_load_directory_raises(tmp_path):
    """A directory is no longer a valid traj_file — must point to a single .jsonl."""
    with pytest.raises(FileNotFoundError):
        SequentialCursor.load(tmp_path)


@pytest.mark.asyncio
async def test_cursor_next_returns_records_in_order(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a", call_id="1"), _record(msg="b", call_id="2")])

    cur = SequentialCursor.load(p)
    first = await cur.next()
    second = await cur.next()

    assert first["id"] == "1"
    assert second["id"] == "2"
    assert cur.position == 2


@pytest.mark.asyncio
async def test_cursor_next_raises_when_exhausted(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="only")])

    cur = SequentialCursor.load(p)
    await cur.next()

    with pytest.raises(CustomLLMError) as exc_info:
        await cur.next()
    assert exc_info.value.status_code == 404
    assert "exhausted" in exc_info.value.message


@pytest.mark.asyncio
async def test_cursor_reset_replays_from_start(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a"), _record(msg="b")])

    cur = SequentialCursor.load(p)
    await cur.next()
    await cur.next()
    cur.reset()

    again = await cur.next()
    assert again["messages"][0]["content"] == "a"


@pytest.mark.asyncio
async def test_cursor_model_mismatch_only_warns(tmp_path, caplog):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a", model="gpt-3.5-turbo")])

    cur = SequentialCursor.load(p)
    record = await cur.next(expected_model="gpt-4o")  # different model -> warn but don't raise
    assert record["id"] == "x"


# ----- TrajectoryReplayer -----


@pytest.mark.asyncio
async def test_replayer_acompletion_returns_recorded_response(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a", call_id="step-1")])

    replayer = TrajectoryReplayer(p)
    response = await replayer.acompletion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "anything"}],
    )

    assert response.id == "step-1"
    assert response.choices[0].message.content == "reply: a"


@pytest.mark.asyncio
async def test_replayer_acompletion_advances_cursor(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(
        p,
        [
            _record(msg="a", call_id="step-1"),
            _record(msg="b", call_id="step-2"),
        ],
    )

    replayer = TrajectoryReplayer(p)
    r1 = await replayer.acompletion(model="gpt-3.5-turbo", messages=[])
    r2 = await replayer.acompletion(model="gpt-3.5-turbo", messages=[])

    assert r1.id == "step-1"
    assert r2.id == "step-2"


@pytest.mark.asyncio
async def test_replayer_astreaming_yields_chunks_that_recompose_the_text(tmp_path):
    """The chunks produced by astreaming should reassemble into the recorded text."""
    p = tmp_path / "traj.jsonl"
    recorded_text = "Hello world, this is a deterministic replay."
    record = _record(msg="hi")
    record["response"]["choices"][0]["message"]["content"] = recorded_text
    _write_jsonl(p, [record])

    replayer = TrajectoryReplayer(p)

    # Build a litellm-shaped ModelResponse mock with one Choice/Delta slot.
    fake_choice = SimpleNamespace(delta=SimpleNamespace(role=None, content=None), index=0)
    fake_response = SimpleNamespace(choices=[fake_choice])

    chunks_text = []
    async for chunk in replayer.astreaming(
        model="gpt-3.5-turbo",
        messages=[],
        model_response=fake_response,
    ):
        if hasattr(chunk, "choices") and chunk.choices and getattr(chunk.choices[0], "delta", None):
            piece = chunk.choices[0].delta.content
            if piece:
                chunks_text.append(piece)

    assert "".join(chunks_text) == recorded_text


@pytest.mark.asyncio
async def test_replayer_acompletion_raises_on_exhaustion(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="only")])

    replayer = TrajectoryReplayer(p)
    await replayer.acompletion(model="gpt-3.5-turbo", messages=[])

    with pytest.raises(CustomLLMError):
        await replayer.acompletion(model="gpt-3.5-turbo", messages=[])
