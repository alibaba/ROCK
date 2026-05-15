"""Tests for TrajectoryRecorder (explicit-call API, no longer a litellm CustomLogger)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from rock.sdk.model.server.traj import TrajectoryRecorder


@pytest.fixture
def mock_monitor():
    monitor = MagicMock()
    with patch(
        "rock.sdk.model.server.traj._get_or_create_metrics_monitor",
        return_value=monitor,
    ):
        yield monitor


def _make_recorder(traj_file) -> TrajectoryRecorder:
    return TrajectoryRecorder(traj_file=traj_file)


@pytest.mark.asyncio
async def test_recorder_appends_each_call_as_jsonl_line(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = _make_recorder(traj_file)

    await recorder.record(
        request={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        response={"id": "a", "choices": []},
        status="success",
        start_time=100.0,
        end_time=100.5,
    )
    await recorder.record(
        request={"model": "gpt-4", "messages": [{"role": "user", "content": "again"}]},
        response={"id": "b", "choices": []},
        status="success",
        start_time=101.0,
        end_time=101.2,
    )

    lines = traj_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["response"]["id"] == "a"
    assert json.loads(lines[1])["response"]["id"] == "b"


@pytest.mark.asyncio
async def test_recorder_writes_request_and_response_verbatim(tmp_path, mock_monitor):
    """Provider-specific fields (reasoning_content, citations, ...) survive untouched."""
    traj_file = tmp_path / "traj.jsonl"
    recorder = _make_recorder(traj_file)

    request = {"model": "glm-5", "stream": True, "messages": [{"role": "user", "content": "你是谁"}]}
    response = {
        "id": "x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "我是 GLM", "reasoning_content": "用户问..."},
                "finish_reason": "stop",
            }
        ],
    }
    await recorder.record(request=request, response=response, status="success", start_time=0.0, end_time=1.0)

    record = json.loads(traj_file.read_text(encoding="utf-8").strip())
    assert record["model"] == "glm-5"
    assert record["stream"] is True
    assert record["request"] == request
    assert record["response"] == response
    assert record["response_time"] == 1.0


@pytest.mark.asyncio
async def test_recorder_emits_metrics_with_status_and_sandbox_id(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = _make_recorder(traj_file)

    with patch.dict("os.environ", {"ROCK_SANDBOX_ID": "sandbox-xyz"}):
        await recorder.record(
            request={"model": "gpt-4"},
            response={"id": "x", "choices": []},
            status="success",
            start_time=0.0,
            end_time=0.5,
        )

    gauge_call = mock_monitor.record_gauge_by_name.call_args
    assert gauge_call[0][0] == "model_service.request.rt"
    assert gauge_call[0][1] == 500.0  # 0.5s -> 500 ms
    assert gauge_call[1]["attributes"]["status"] == "success"
    assert gauge_call[1]["attributes"]["sandbox_id"] == "sandbox-xyz"
    assert gauge_call[1]["attributes"]["type"] == "chat_completions"

    mock_monitor.record_counter_by_name.assert_called_once_with(
        "model_service.request.count", 1, attributes=gauge_call[1]["attributes"]
    )


@pytest.mark.asyncio
async def test_recorder_records_failure_with_error_text(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = _make_recorder(traj_file)

    await recorder.record(
        request={"model": "gpt-4"},
        response=None,
        status="failure",
        start_time=0.0,
        end_time=1.0,
        error="upstream_status=429",
    )

    record = json.loads(traj_file.read_text(encoding="utf-8").strip())
    assert record["status"] == "failure"
    assert record["error"] == "upstream_status=429"
    assert record["response"] is None

    gauge_call = mock_monitor.record_gauge_by_name.call_args
    assert gauge_call[1]["attributes"]["status"] == "failure"


@pytest.mark.asyncio
async def test_recorder_creates_parent_directory(tmp_path, mock_monitor):
    traj_file = tmp_path / "deep" / "nested" / "traj.jsonl"
    recorder = _make_recorder(traj_file)

    await recorder.record(
        request={"model": "gpt-4"},
        response={"id": "x", "choices": []},
        status="success",
        start_time=0.0,
        end_time=0.5,
    )

    assert traj_file.exists()
    assert traj_file.parent.is_dir()
