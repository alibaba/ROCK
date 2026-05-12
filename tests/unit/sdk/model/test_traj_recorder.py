"""Tests for TrajectoryRecorder (litellm CustomLogger that writes JSONL + emits OTLP metrics)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from rock.sdk.model.server.integrations.traj_recorder import TrajectoryRecorder


def _sample_payload(**overrides):
    payload = {
        "id": "chatcmpl-abc",
        "trace_id": "trace-1",
        "call_type": "acompletion",
        "stream": False,
        "status": "success",
        "model": "gpt-3.5-turbo",
        "model_id": None,
        "model_group": None,
        "api_base": "https://api.openai.com/v1",
        "messages": [{"role": "user", "content": "hi"}],
        "response": {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello back"},
                    "finish_reason": "stop",
                }
            ],
        },
        "model_parameters": {"temperature": 0.7},
        "startTime": 100.0,
        "endTime": 100.5,
        "completionStartTime": 100.5,
        "response_time": 0.5,
        "total_tokens": 12,
        "prompt_tokens": 4,
        "completion_tokens": 8,
        "metadata": {},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def mock_monitor():
    monitor = MagicMock()
    with patch(
        "rock.sdk.model.server.integrations.traj_recorder._get_or_create_metrics_monitor",
        return_value=monitor,
    ):
        yield monitor


@pytest.mark.asyncio
async def test_recorder_appends_each_call_as_jsonl_line(tmp_path, mock_monitor):
    """Each successful call adds one JSONL line (always append-only)."""
    traj_file = tmp_path / "traj.jsonl"
    recorder = TrajectoryRecorder(traj_file=traj_file)

    payload_a = _sample_payload(id="a", trace_id="run-1")
    payload_b = _sample_payload(id="b", trace_id="run-1")

    await recorder.async_log_success_event(
        kwargs={"standard_logging_object": payload_a}, response_obj=None, start_time=0, end_time=1
    )
    await recorder.async_log_success_event(
        kwargs={"standard_logging_object": payload_b}, response_obj=None, start_time=0, end_time=1
    )

    lines = traj_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "a"
    assert json.loads(lines[1])["id"] == "b"


@pytest.mark.asyncio
async def test_recorder_emits_metrics_with_sandbox_id(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = TrajectoryRecorder(traj_file=traj_file)

    with patch.dict("os.environ", {"ROCK_SANDBOX_ID": "sandbox-xyz"}):
        await recorder.async_log_success_event(
            kwargs={"standard_logging_object": _sample_payload()},
            response_obj=None,
            start_time=0,
            end_time=1,
        )

    mock_monitor.record_gauge_by_name.assert_called_once()
    gauge_args = mock_monitor.record_gauge_by_name.call_args
    assert gauge_args.args[0] == "model_service.request.rt"
    # response_time of 0.5s → 500 ms
    assert gauge_args.args[1] == 500.0
    assert gauge_args.kwargs["attributes"]["status"] == "success"
    assert gauge_args.kwargs["attributes"]["sandbox_id"] == "sandbox-xyz"
    assert gauge_args.kwargs["attributes"]["type"] == "chat_completions"

    mock_monitor.record_counter_by_name.assert_called_once_with(
        "model_service.request.count", 1, attributes=gauge_args.kwargs["attributes"]
    )


@pytest.mark.asyncio
async def test_recorder_records_failure_with_failure_status(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = TrajectoryRecorder(traj_file=traj_file)

    failed_payload = _sample_payload(status="failure", error_information={"error_class": "RateLimitError"})

    await recorder.async_log_failure_event(
        kwargs={"standard_logging_object": failed_payload},
        response_obj=None,
        start_time=0,
        end_time=1,
    )

    lines = traj_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "failure"

    gauge_args = mock_monitor.record_gauge_by_name.call_args
    assert gauge_args.kwargs["attributes"]["status"] == "failure"


@pytest.mark.asyncio
async def test_recorder_skips_when_payload_missing(tmp_path, mock_monitor):
    """If litellm doesn't attach a standard_logging_object, the recorder no-ops."""
    traj_file = tmp_path / "traj.jsonl"
    recorder = TrajectoryRecorder(traj_file=traj_file)

    await recorder.async_log_success_event(kwargs={}, response_obj=None, start_time=0, end_time=1)

    assert not traj_file.exists() or traj_file.read_text() == ""
    mock_monitor.record_gauge_by_name.assert_not_called()
    mock_monitor.record_counter_by_name.assert_not_called()


@pytest.mark.asyncio
async def test_recorder_creates_parent_directory(tmp_path, mock_monitor):
    traj_file = tmp_path / "deep" / "nested" / "traj.jsonl"

    recorder = TrajectoryRecorder(traj_file=traj_file)
    await recorder.async_log_success_event(
        kwargs={"standard_logging_object": _sample_payload()},
        response_obj=None,
        start_time=0,
        end_time=1,
    )

    assert traj_file.exists()
    assert traj_file.parent.is_dir()


@pytest.mark.asyncio
async def test_recorder_falls_back_to_start_end_time_when_response_time_missing(tmp_path, mock_monitor):
    traj_file = tmp_path / "traj.jsonl"
    recorder = TrajectoryRecorder(traj_file=traj_file)

    payload = _sample_payload(startTime=10.0, endTime=10.25)
    payload.pop("response_time", None)

    await recorder.async_log_success_event(
        kwargs={"standard_logging_object": payload}, response_obj=None, start_time=0, end_time=1
    )

    gauge_args = mock_monitor.record_gauge_by_name.call_args
    assert abs(gauge_args.args[1] - 250.0) < 1e-6
