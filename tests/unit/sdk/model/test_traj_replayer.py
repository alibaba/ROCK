"""Tests for SequentialCursor (the replay cursor used by proxy.py).

The proxy serves replay responses directly — there is no CustomLLM-based
``TrajectoryReplayer`` anymore. End-to-end replay coverage (cursor + SSE chunk
emit + cursor-exhausted → 404) lives in ``test_proxy.py``.
"""

import json

import pytest

from rock.sdk.model.server.traj import SequentialCursor, TrajectoryExhausted


def _record(*, msg: str, model: str = "gpt-3.5-turbo", call_id: str = "x") -> dict:
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
    """Path must be a single .jsonl file, not a directory."""
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
async def test_cursor_next_raises_trajectory_exhausted_when_done(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="only")])

    cur = SequentialCursor.load(p)
    await cur.next()

    with pytest.raises(TrajectoryExhausted) as exc_info:
        await cur.next()
    assert exc_info.value.position == 1
    assert exc_info.value.total == 1


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
async def test_cursor_model_mismatch_only_warns(tmp_path):
    p = tmp_path / "traj.jsonl"
    _write_jsonl(p, [_record(msg="a", model="gpt-3.5-turbo")])

    cur = SequentialCursor.load(p)
    record = await cur.next(expected_model="gpt-4o")  # different model -> warn but don't raise
    assert record["id"] == "x"
