"""Unit tests for rock.cli.command.model_service.ModelServiceCommand.

Drive the sub-parser end-to-end with argparse so the surface that users
actually type at the terminal is what we exercise. ``ModelService.start`` is
mocked — these tests assert wiring (argparse → handler → SDK call), not the
subprocess command construction (covered separately in
tests/unit/sdk/model/test_service.py).
"""

from __future__ import annotations

import argparse
import asyncio
from unittest.mock import AsyncMock

import pytest

from rock.cli.command.model_service import ModelServiceCommand


def _build_parser() -> argparse.ArgumentParser:
    """Top-level parser with `model-service` subcommand wired in, same as the CLI."""
    top = argparse.ArgumentParser(prog="rock")
    subparsers = top.add_subparsers(dest="command")
    asyncio.run(ModelServiceCommand.add_parser_to(subparsers))
    return top


@pytest.fixture
def isolate_pid_file(monkeypatch, tmp_path):
    """Redirect PID dir/file into tmp so arun() doesn't touch ./data/cli/model."""
    monkeypatch.setattr(ModelServiceCommand, "DEFAULT_MODEL_SERVICE_DIR", str(tmp_path))
    monkeypatch.setattr(ModelServiceCommand, "DEFAULT_MODEL_SERVICE_PID_FILE", str(tmp_path / "pid.txt"))


@pytest.fixture
def fake_start(monkeypatch):
    """Replace ModelService.start with an AsyncMock returning a fixed pid."""
    mock = AsyncMock(return_value="12345")
    monkeypatch.setattr("rock.cli.command.model_service.ModelService.start", mock)
    return mock


# ---------- argparse: the new flags must parse ----------


def test_recording_file_flag_parses():
    parser = _build_parser()
    ns = parser.parse_args(["model-service", "start", "--type", "proxy", "--recording-file", "/tmp/out.jsonl"])
    assert ns.recording_file == "/tmp/out.jsonl"
    assert ns.replay_file is None


def test_replay_file_flag_parses():
    parser = _build_parser()
    ns = parser.parse_args(["model-service", "start", "--type", "proxy", "--replay-file", "/tmp/in.jsonl"])
    assert ns.replay_file == "/tmp/in.jsonl"
    assert ns.recording_file is None


def test_neither_flag_defaults_to_none():
    parser = _build_parser()
    ns = parser.parse_args(["model-service", "start", "--type", "proxy"])
    assert ns.recording_file is None
    assert ns.replay_file is None


# ---------- handler: passes parsed args through to ModelService.start ----------


def test_start_handler_forwards_recording_file(isolate_pid_file, fake_start):
    parser = _build_parser()
    ns = parser.parse_args(
        [
            "model-service",
            "start",
            "--type",
            "proxy",
            "--proxy-base-url",
            "https://api.openai.com/v1",
            "--recording-file",
            "/tmp/out.jsonl",
        ]
    )
    asyncio.run(ModelServiceCommand().arun(ns))

    kwargs = fake_start.call_args.kwargs
    assert kwargs["recording_file"] == "/tmp/out.jsonl"
    assert kwargs["replay_file"] is None
    assert kwargs["proxy_base_url"] == "https://api.openai.com/v1"
    assert kwargs["model_service_type"] == "proxy"


def test_start_handler_forwards_replay_file(isolate_pid_file, fake_start):
    parser = _build_parser()
    ns = parser.parse_args(
        [
            "model-service",
            "start",
            "--type",
            "proxy",
            "--replay-file",
            "/tmp/in.jsonl",
        ]
    )
    asyncio.run(ModelServiceCommand().arun(ns))

    kwargs = fake_start.call_args.kwargs
    assert kwargs["replay_file"] == "/tmp/in.jsonl"
    assert kwargs["recording_file"] is None


def test_start_handler_omits_both_when_unset(isolate_pid_file, fake_start):
    parser = _build_parser()
    ns = parser.parse_args(["model-service", "start", "--type", "proxy"])
    asyncio.run(ModelServiceCommand().arun(ns))

    kwargs = fake_start.call_args.kwargs
    assert kwargs["recording_file"] is None
    assert kwargs["replay_file"] is None


# ---------- anti-call-llm: --response-file ----------


def test_response_file_flag_parses():
    parser = _build_parser()
    ns = parser.parse_args(["model-service", "anti-call-llm", "--index", "1", "--response-file", "/tmp/resp.json"])
    assert ns.response_file == "/tmp/resp.json"
    assert ns.response is None


def test_response_and_response_file_are_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "model-service", "anti-call-llm", "--index", "1",
            "--response", '{"foo":"bar"}', "--response-file", "/tmp/resp.json",
        ])


def test_anti_call_llm_reads_response_file(monkeypatch, tmp_path):
    payload = '{"id":"chatcmpl-1","choices":[{"message":{"role":"assistant","content":"hello"}}]}'
    resp_file = tmp_path / "resp.json"
    resp_file.write_text(payload)

    mock_anti_call = AsyncMock(return_value="next_request_json")
    monkeypatch.setattr("rock.cli.command.model_service.ModelClient.anti_call_llm", mock_anti_call)

    parser = _build_parser()
    ns = parser.parse_args(["model-service", "anti-call-llm", "--index", "1", "--response-file", str(resp_file)])
    asyncio.run(ModelServiceCommand().arun(ns))

    mock_anti_call.assert_called_once_with(index=1, last_response=payload)
