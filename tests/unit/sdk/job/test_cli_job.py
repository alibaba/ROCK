"""Tests for rock.cli.command.job — JobCommand with --type bash/harbor routing."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.cli.command.job import JobCommand


async def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="main_command")
    await JobCommand.add_parser_to(sub)
    return p


def _make_mock_job_result():
    mock_result = MagicMock()
    mock_result.trial_results = []
    mock_result.status = "completed"
    return mock_result


# ----------------------------------------------------------------------------
# Parser tests
# ----------------------------------------------------------------------------


async def test_parser_default_type_is_bash():
    p = await _build_parser()
    args = p.parse_args(["job", "run", "--script-content", "echo hi"])
    assert args.type == "bash"
    assert args.script_content == "echo hi"


async def test_parser_accepts_type_bash_explicit():
    p = await _build_parser()
    args = p.parse_args(["job", "run", "--type", "bash", "--script-content", "echo hi"])
    assert args.type == "bash"


async def test_parser_accepts_type_harbor():
    p = await _build_parser()
    args = p.parse_args(["job", "run", "--type", "harbor", "--config", "/tmp/c.yaml"])
    assert args.type == "harbor"
    assert args.config == "/tmp/c.yaml"


async def test_parser_supports_all_bash_args():
    p = await _build_parser()
    args = p.parse_args(
        [
            "job",
            "run",
            "--script",
            "/tmp/s.sh",
            "--image",
            "python:3.11",
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--timeout",
            "600",
            "--local-path",
            "/tmp/local",
            "--target-path",
            "/root/other",
        ]
    )
    assert args.script == "/tmp/s.sh"
    assert args.image == "python:3.11"
    assert args.memory == "4g"
    assert args.cpus == 2.0
    assert args.timeout == 600
    assert args.local_path == "/tmp/local"
    assert args.target_path == "/root/other"


async def test_parser_invalid_type_rejected():
    p = await _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["job", "run", "--type", "invalid"])


# ----------------------------------------------------------------------------
# arun / _job_run behavior tests
# ----------------------------------------------------------------------------


def _bash_args(**overrides):
    defaults = dict(
        job_command="run",
        type="bash",
        script=None,
        script_content=None,
        image=None,
        memory=None,
        cpus=None,
        local_path=None,
        target_path="/root/job",
        timeout=3600,
        config=None,
        base_url=None,
        cluster=None,
        extra_headers=None,
        env=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


async def test_bash_creates_bash_job_config_and_runs():
    from rock.sdk.job.config import BashJobConfig

    args = _bash_args(
        script_content="echo hello",
        image="python:3.11",
        memory="4g",
        cpus=2.0,
        timeout=600,
    )

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        MockJob.assert_called_once()
        config_arg = MockJob.call_args[0][0]
        assert isinstance(config_arg, BashJobConfig)
        assert config_arg.script == "echo hello"
        assert config_arg.script_path is None
        assert config_arg.environment.image == "python:3.11"
        assert config_arg.environment.memory == "4g"
        assert config_arg.environment.cpus == 2.0
        assert config_arg.timeout == 600
        assert config_arg.environment.auto_stop is True
        mock_instance.run.assert_awaited_once()


async def test_bash_with_script_path():
    from rock.sdk.job.config import BashJobConfig

    args = _bash_args(script="/tmp/my_script.sh")

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert isinstance(config_arg, BashJobConfig)
        assert config_arg.script_path == "/tmp/my_script.sh"
        assert config_arg.script is None


async def test_bash_with_file_upload():
    args = _bash_args(
        script_content="echo hi",
        local_path="/tmp/src",
        target_path="/root/target",
    )

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert config_arg.environment.uploads == [("/tmp/src", "/root/target")]


async def test_bash_requires_script_or_script_content():
    args = _bash_args()  # neither set

    with patch("rock.sdk.job.Job") as MockJob, patch("rock.cli.command.job.logger") as mock_logger:
        cmd = JobCommand()
        await cmd.arun(args)

        MockJob.assert_not_called()
        mock_logger.error.assert_called()


async def test_bash_rejects_both_script_and_script_content():
    args = _bash_args(script="/tmp/s.sh", script_content="echo hi")

    with patch("rock.sdk.job.Job") as MockJob, patch("rock.cli.command.job.logger") as mock_logger:
        cmd = JobCommand()
        await cmd.arun(args)

        MockJob.assert_not_called()
        mock_logger.error.assert_called()


async def test_harbor_requires_config():
    args = _bash_args(type="harbor", config=None)

    with patch("rock.sdk.job.Job") as MockJob, patch("rock.cli.command.job.logger") as mock_logger:
        cmd = JobCommand()
        await cmd.arun(args)

        MockJob.assert_not_called()
        mock_logger.error.assert_called()


async def test_harbor_loads_from_yaml():
    from rock.sdk.bench.models.job.config import HarborJobConfig

    yaml_content = """
experiment_id: exp-123
job_name: my-harbor-job
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        yaml_path = f.name

    try:
        args = _bash_args(type="harbor", config=yaml_path)

        with patch("rock.sdk.job.Job") as MockJob:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
            MockJob.return_value = mock_instance

            cmd = JobCommand()
            await cmd.arun(args)

            MockJob.assert_called_once()
            config_arg = MockJob.call_args[0][0]
            assert isinstance(config_arg, HarborJobConfig)
            assert config_arg.experiment_id == "exp-123"
            assert config_arg.environment.auto_stop is True
    finally:
        Path(yaml_path).unlink(missing_ok=True)


async def test_harbor_image_override():
    yaml_content = """
experiment_id: exp-abc
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        yaml_path = f.name

    try:
        args = _bash_args(type="harbor", config=yaml_path, image="custom:tag")

        with patch("rock.sdk.job.Job") as MockJob:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
            MockJob.return_value = mock_instance

            cmd = JobCommand()
            await cmd.arun(args)

            config_arg = MockJob.call_args[0][0]
            assert config_arg.environment.image == "custom:tag"
    finally:
        Path(yaml_path).unlink(missing_ok=True)


async def test_parser_accepts_base_url_and_cluster():
    p = await _build_parser()
    args = p.parse_args(
        [
            "job",
            "run",
            "--script-content",
            "echo hi",
            "--base-url",
            "http://example.com",
            "--cluster",
            "test-cluster-a",
        ]
    )
    assert args.base_url == "http://example.com"
    assert args.cluster == "test-cluster-a"


async def test_bash_passes_base_url_and_cluster_to_environment():
    args = _bash_args(
        script_content="echo hello",
        base_url="http://example.com",
        cluster="test-cluster-a",
    )

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert config_arg.environment.base_url == "http://example.com"
        assert config_arg.environment.cluster == "test-cluster-a"


async def test_parser_accepts_env_args():
    p = await _build_parser()
    args = p.parse_args(
        [
            "job",
            "run",
            "--script-content",
            "echo hi",
            "--env",
            "FOO=bar",
            "--env",
            "BAZ=qux=123",
        ]
    )
    assert args.env == ["FOO=bar", "BAZ=qux=123"]


async def test_parser_env_default_is_none():
    p = await _build_parser()
    args = p.parse_args(["job", "run", "--script-content", "echo hi"])
    assert args.env is None


async def test_bash_passes_env_to_config():
    args = _bash_args(script_content="echo hello")
    args.env = ["SERP_DEV_KEY=abc123", "RUN_CMD=claw-eval batch --parallel 4"]

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert config_arg.environment.env == {
            "SERP_DEV_KEY": "abc123",
            "RUN_CMD": "claw-eval batch --parallel 4",
        }


async def test_bash_env_with_equals_in_value():
    """Values containing '=' should not be split."""
    args = _bash_args(script_content="echo hello")
    args.env = ["API_KEY=sk-abc=def=="]

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert config_arg.environment.env == {"API_KEY": "sk-abc=def=="}


async def test_bash_no_env_defaults_to_empty():
    args = _bash_args(script_content="echo hello")

    with patch("rock.sdk.job.Job") as MockJob:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=_make_mock_job_result())
        MockJob.return_value = mock_instance

        cmd = JobCommand()
        await cmd.arun(args)

        config_arg = MockJob.call_args[0][0]
        assert config_arg.environment.env == {}


async def test_unknown_job_command_logs_error():
    args = argparse.Namespace(job_command="weird")

    with patch("rock.cli.command.job.logger") as mock_logger:
        cmd = JobCommand()
        await cmd.arun(args)
        mock_logger.error.assert_called()
