from __future__ import annotations

import argparse
import asyncio
from unittest.mock import MagicMock

import pytest

from rock.cli.command.job import JobCommand


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="rock")
    subparsers = top.add_subparsers(dest="command")
    asyncio.run(JobCommand.add_parser_to(subparsers))
    return top


def test_job_config_hyphen_alias():
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml", "--task", "t1"])
    assert ns.job_config == "foo.yaml"
    assert ns.task == "t1"


def test_run_all_subcommand_is_removed():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["job", "run-all", "--job-config", "foo.yaml"])


def test_run_parser_supports_single_multi_full_and_explicit_resume():
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml", "--tasks", "t1,t2", "--concurrency", "3"])
    assert ns.job_command == "run"
    assert ns.tasks == "t1,t2"
    assert ns.concurrency == 3

    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml", "--all", "--limit", "2", "--jsonl"])
    assert ns.all is True
    assert ns.limit == 2
    assert ns.jsonl is True

    ns = parser.parse_args(
        [
            "job",
            "run",
            "--job-config",
            "foo.yaml",
            "--task",
            "t1",
            "--job-name",
            "old-job",
            "--resume-sandbox-id",
            "sb-1",
            "--resume-pid",
            "42",
        ]
    )
    assert ns.resume_sandbox_id == "sb-1"
    assert ns.resume_pid == 42
    assert ns.resume_session is None
    assert ns.job_name == "old-job"


def test_run_query_parsers_use_explicit_command_names():
    parser = _build_parser()
    job_list = parser.parse_args(["job", "job-list", "--namespace", "ns", "--experiment-id", "exp"])
    assert job_list.job_command == "job-list"

    show = parser.parse_args(["job", "job-show", "job-1", "--job-config", "foo.yaml"])
    assert show.job_command == "job-show"
    assert show.job_name == "job-1"

    trial_list = parser.parse_args(["job", "trial-list", "job-1", "--namespace", "ns", "--experiment-id", "exp"])
    assert trial_list.job_command == "trial-list"
    assert trial_list.job_name == "job-1"

    trial_show = parser.parse_args(
        ["job", "trial-show", "job-1", "trial-1", "--namespace", "ns", "--experiment-id", "exp"]
    )
    assert trial_show.job_command == "trial-show"
    assert trial_show.job_name == "job-1"
    assert trial_show.trial_name == "trial-1"


def test_old_query_subcommands_are_removed():
    parser = _build_parser()
    for subcommand in ["run-list", "run-status", "runs", "status", "list", "show", "trials", "trial"]:
        with pytest.raises(SystemExit):
            parser.parse_args(["job", subcommand])

    with pytest.raises(SystemExit):
        parser.parse_args(["job", "run", "--resume", "run-1"])


def test_job_help_uses_self_describing_query_command_summaries(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["job", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "run-list" not in out
    assert "run-status" not in out
    assert "job-list" in out
    assert "List job artifact directories in an experiment" in out
    assert "job-show" in out
    assert "Show one job artifact by job name" in out
    assert "trial-list" in out
    assert "List trial results under one job artifact" in out
    assert "trial-show" in out
    assert "Show one trial result and verifier details" in out


def test_query_subcommand_help_explains_locators_and_identifiers(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["job", "job-show", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "Show one job artifact by job name" in out
    assert "YAML config used to locate OSS artifacts" in out
    assert "Run id" not in out
    assert "Task id inside the run" not in out
    assert "Job artifact name" in out

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["job", "trial-show", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "Show one trial result and verifier details" in out
    assert "Job artifact name" in out
    assert "Trial name under the job artifact" in out


class TestRunValidation:
    @pytest.fixture(autouse=True)
    def _parser(self):
        self.top = _build_parser()

    def _run(self, argv):
        ns = self.top.parse_args(argv)
        asyncio.run(JobCommand().arun(ns))

    def test_missing_definition_for_fresh_run_errors(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--task", "t1"])
        assert excinfo.value.code == 2
        assert "Missing job definition" in capsys.readouterr().err

    def test_script_requires_explicit_task(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--script-content", "echo hi"])
        assert excinfo.value.code == 2
        assert "fresh run requires an explicit task" in capsys.readouterr().err

    def test_task_selection_arguments_are_mutually_exclusive(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--script-content", "echo hi", "--task", "t1", "--all"])
        assert excinfo.value.code == 2

    def test_resume_requires_explicit_task(self, monkeypatch, capsys):
        from rock.sdk.job.config import BashJobConfig

        config = BashJobConfig(job_name="old-job", script="echo hi")
        monkeypatch.setattr(JobCommand, "_config_from_yaml", lambda self, parser, args: config)

        with pytest.raises(SystemExit) as excinfo:
            self._run(
                [
                    "job",
                    "run",
                    "--job-config",
                    "foo.yaml",
                    "--resume-sandbox-id",
                    "sb-1",
                    "--resume-pid",
                    "42",
                ]
            )
        assert excinfo.value.code == 2
        assert "requires exactly one explicit --task" in capsys.readouterr().err

    def test_resume_requires_pid(self, monkeypatch, capsys):
        from rock.sdk.job.config import BashJobConfig

        monkeypatch.setattr(
            JobCommand,
            "_config_from_yaml",
            lambda self, parser, args: BashJobConfig(job_name="old-job", script="echo hi"),
        )

        with pytest.raises(SystemExit) as excinfo:
            self._run(
                [
                    "job",
                    "run",
                    "--job-config",
                    "foo.yaml",
                    "--task",
                    "t1",
                    "--resume-sandbox-id",
                    "sb-1",
                ]
            )
        assert excinfo.value.code == 2
        assert "--resume-pid is required" in capsys.readouterr().err

    def test_resume_details_require_sandbox_id(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(
                [
                    "job",
                    "run",
                    "--script-content",
                    "echo hi",
                    "--task",
                    "t1",
                    "--resume-pid",
                    "42",
                ]
            )
        assert excinfo.value.code == 2
        assert "require --resume-sandbox-id" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "extra_args",
        [
            ["--tasks", "t1,t2"],
            ["--all"],
            ["--limit", "1"],
            ["--concurrency", "2"],
        ],
    )
    def test_resume_rejects_multi_task_options(self, monkeypatch, capsys, extra_args):
        from rock.sdk.job.config import BashJobConfig

        monkeypatch.setattr(
            JobCommand,
            "_config_from_yaml",
            lambda self, parser, args: BashJobConfig(job_name="old-job", script="echo hi"),
        )

        with pytest.raises(SystemExit) as excinfo:
            self._run(
                [
                    "job",
                    "run",
                    "--job-config",
                    "foo.yaml",
                    "--task",
                    "t1",
                    "--resume-sandbox-id",
                    "sb-1",
                    "--resume-pid",
                    "42",
                    *extra_args,
                ]
            )
        assert excinfo.value.code == 2
        assert "single-task resume" in capsys.readouterr().err

    def test_resume_flags_mode_requires_job_name(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(
                [
                    "job",
                    "run",
                    "--script-content",
                    "echo hi",
                    "--task",
                    "t1",
                    "--resume-sandbox-id",
                    "sb-1",
                    "--resume-pid",
                    "42",
                ]
            )
        assert excinfo.value.code == 2
        assert "--job-name is required" in capsys.readouterr().err


class TestRunEndToEnd:
    def test_flags_mode_builds_bash_config_and_uses_unified_handler(self, monkeypatch):
        from rock.sdk.job.config import BashJobConfig

        captured = {}

        class FakeHandler:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            async def run(self, cfg):
                captured["cfg"] = cfg
                return type("R", (), {"failed": 0, "run_id": "run-1"})()

        monkeypatch.setattr("rock.cli.job_run.UnifiedJobRunHandler", FakeHandler)

        parser = _build_parser()
        ns = parser.parse_args(["job", "run", "--script-content", "echo hi", "--task", "task-1"])
        asyncio.run(JobCommand().arun(ns))

        assert isinstance(captured["cfg"], BashJobConfig)
        assert captured["kwargs"]["mode"] == "single"
        assert captured["kwargs"]["task_ids"] == ["task-1"]

    @pytest.mark.parametrize(
        ("session_args", "expected_session"),
        [
            ([], "rock-job-old-job"),
            (["--resume-session", "custom-session"], "custom-session"),
        ],
    )
    def test_resume_builds_explicit_handle_and_derives_optional_session(
        self,
        monkeypatch,
        session_args,
        expected_session,
    ):
        from rock.sdk.job.config import BashJobConfig

        captured = {}

        class FakeHandler:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            async def run(self, cfg):
                captured["cfg"] = cfg
                return type("R", (), {"failed": 0, "run_id": "local-run"})()

        monkeypatch.setattr(
            JobCommand,
            "_config_from_yaml",
            lambda self, parser, args: BashJobConfig(job_name="old-job", script="echo hi"),
        )
        monkeypatch.setattr("rock.cli.job_run.UnifiedJobRunHandler", FakeHandler)

        parser = _build_parser()
        ns = parser.parse_args(
            [
                "job",
                "run",
                "--job-config",
                "foo.yaml",
                "--task",
                "t1",
                "--resume-sandbox-id",
                "sb-1",
                "--resume-pid",
                "42",
                *session_args,
            ]
        )
        asyncio.run(JobCommand().arun(ns))

        handle = captured["kwargs"]["resume_handle"]
        assert handle.sandbox_id == "sb-1"
        assert handle.pid == 42
        assert handle.session == expected_session


class TestArtifactQueries:
    def test_job_show_reads_result_by_explicit_job_name(self, monkeypatch, capsys):
        viewer = MagicMock()
        viewer.get_job_result.return_value = {
            "id": "job-id",
            "started_at": "start",
            "finished_at": "finish",
            "n_total_trials": 1,
        }
        monkeypatch.setattr(JobCommand, "_build_viewer_from_locator", lambda self, args: viewer)
        parser = _build_parser()
        ns = parser.parse_args(["job", "job-show", "job-1"])

        asyncio.run(JobCommand().arun(ns))

        out = capsys.readouterr().out
        assert "Job: job-1" in out
        assert "id: job-id" in out
        viewer.get_job_result.assert_called_once_with("job-1")

    def test_job_config_locator_builds_viewer_directly_from_oss_mirror(self, monkeypatch):
        from rock.sdk.envhub.config import OssMirrorConfig
        from rock.sdk.job.config import BashJobConfig

        mirror = OssMirrorConfig(
            enabled=True,
            namespace="ns",
            experiment_id="exp",
            oss_bucket="bucket",
            oss_endpoint="endpoint",
        )
        config = BashJobConfig(job_name="job-1", script="echo hi", environment={"oss_mirror": mirror})
        expected_viewer = MagicMock()
        from_oss_mirror = MagicMock(return_value=expected_viewer)
        monkeypatch.setattr(JobCommand, "_config_from_yaml", lambda self, parser, args: config)
        monkeypatch.setattr("rock.sdk.job.viewer.JobViewer.from_oss_mirror", from_oss_mirror)

        args = argparse.Namespace(job_config="foo.yaml")
        viewer = JobCommand()._build_viewer_from_locator(args)

        assert viewer is expected_viewer
        from_oss_mirror.assert_called_once_with(mirror)
