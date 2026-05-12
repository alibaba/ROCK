"""Tests for ModelService.start_sandbox_service subprocess command construction.

Covers the CLI flag wiring without actually spawning a subprocess: mock Popen
and inspect the argv it would have been called with.
"""

from unittest.mock import patch

from rock.sdk.model.service import ModelService


def _captured_argv(**start_kwargs) -> list[str]:
    with patch("rock.sdk.model.service.subprocess.Popen") as mock_popen:
        ModelService().start_sandbox_service(**start_kwargs)
    return mock_popen.call_args[0][0]


def test_start_sandbox_service_omits_recording_and_replay_flags_by_default():
    argv = _captured_argv(model_service_type="proxy", proxy_base_url="https://api.openai.com/v1", port=8080)
    assert argv[1:5] == ["-m", "main", "--type", "proxy"]
    assert "--proxy-base-url" in argv and "https://api.openai.com/v1" in argv
    assert "--port" in argv and "8080" in argv
    assert "--recording-file" not in argv
    assert "--replay-file" not in argv


def test_start_sandbox_service_passes_recording_file():
    argv = _captured_argv(model_service_type="proxy", recording_file="/tmp/my-traj.jsonl")
    idx = argv.index("--recording-file")
    assert argv[idx + 1] == "/tmp/my-traj.jsonl"
    assert "--replay-file" not in argv


def test_start_sandbox_service_passes_replay_file():
    argv = _captured_argv(model_service_type="proxy", replay_file="/tmp/in.jsonl")
    idx = argv.index("--replay-file")
    assert argv[idx + 1] == "/tmp/in.jsonl"
    assert "--recording-file" not in argv
