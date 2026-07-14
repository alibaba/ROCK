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


def test_run_parser_supports_single_multi_full_and_resume():
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml", "--tasks", "t1,t2", "--concurrency", "3"])
    assert ns.job_command == "run"
    assert ns.tasks == "t1,t2"
    assert ns.concurrency == 3

    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml", "--all", "--limit", "2", "--jsonl"])
    assert ns.all is True
    assert ns.limit == 2
    assert ns.jsonl is True

    ns = parser.parse_args(["job", "run", "--resume", "run-1", "--job-config", "foo.yaml"])
    assert ns.resume == "run-1"


def test_run_query_parsers_use_explicit_command_names():
    parser = _build_parser()
    runs = parser.parse_args(["job", "run-list", "--job-config", "foo.yaml", "--output", "json"])
    assert runs.job_command == "run-list"
    assert runs.job_config == "foo.yaml"
    assert runs.output == "json"

    status = parser.parse_args(["job", "run-status", "--run-id", "run-1", "--job-config", "foo.yaml", "--jobs"])
    assert status.job_command == "run-status"
    assert status.run_id == "run-1"
    assert status.jobs is True

    job_list = parser.parse_args(["job", "job-list", "--namespace", "ns", "--experiment-id", "exp"])
    assert job_list.job_command == "job-list"

    show = parser.parse_args(["job", "job-show", "--run-id", "run-1", "--task-id", "t1", "--job-config", "foo.yaml"])
    assert show.job_command == "job-show"
    assert show.run_id == "run-1"
    assert show.task_id == "t1"

    trial_list = parser.parse_args(["job", "trial-list", "job-1", "--namespace", "ns", "--experiment-id", "exp"])
    assert trial_list.job_command == "trial-list"
    assert trial_list.job_name == "job-1"

    trial_show = parser.parse_args(["job", "trial-show", "job-1", "trial-1", "--namespace", "ns", "--experiment-id", "exp"])
    assert trial_show.job_command == "trial-show"
    assert trial_show.job_name == "job-1"
    assert trial_show.trial_name == "trial-1"


def test_old_query_subcommands_are_removed():
    parser = _build_parser()
    for subcommand in ["runs", "status", "list", "show", "trials", "trial"]:
        with pytest.raises(SystemExit):
            parser.parse_args(["job", subcommand])


def test_job_help_uses_self_describing_query_command_summaries(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["job", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "run-list" in out
    assert "List historical job runs from run metadata" in out
    assert "run-status" in out
    assert "Show summary and task/job status for one run" in out
    assert "job-list" in out
    assert "List job artifact directories in an experiment" in out
    assert "job-show" in out
    assert "Show one job artifact by job name or run/task id" in out
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
    assert "Show one job artifact by job name or run/task id" in out
    assert "YAML config used to locate OSS artifacts" in out
    assert "Run id from rock job run or run-list" in out
    assert "Task id inside the run; use with --run-id" in out
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

    def test_resume_rejects_task_selection(self, monkeypatch, capsys):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.meta import RunMeta

        config = BashJobConfig(script="echo hi", environment={"oss_mirror": {"enabled": True, "namespace": "ns", "experiment_id": "exp", "oss_bucket": "b", "oss_endpoint": "e"}})

        class FakeRepo:
            _viewer = MagicMock()

            def get(self, run_id):
                return RunMeta(run_id=run_id, mode="single", status="running", total_tasks=1, pending_tasks=1)

            def find_completed_tasks(self, run_id):
                return set()

        monkeypatch.setattr(JobCommand, "_config_from_yaml", lambda self, parser, args: config)
        monkeypatch.setattr("rock.sdk.job.run_meta.RunMetaRepository.from_job_config", classmethod(lambda cls, cfg: FakeRepo()))

        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--resume", "run-1", "--job-config", "foo.yaml", "--task", "t1"])
        assert excinfo.value.code == 2
        assert "resume cannot be combined" in capsys.readouterr().err


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


class TestRunQueries:
    def test_run_list_prints_run_meta(self, monkeypatch, capsys):
        from rock.sdk.job.meta import RunMeta, RunScoreSummary

        viewer = MagicMock()
        viewer.list_runs.return_value = [
            RunMeta(
                run_id="run-1",
                mode="full",
                dataset="org/ds",
                split="test",
                total_tasks=2,
                pending_tasks=0,
                status="completed",
                summary=RunScoreSummary(completed=2, failed=0, skipped=0, avg_score=1.0, total_score=2.0, pass_rate=1.0),
            )
        ]
        monkeypatch.setattr(JobCommand, "_build_viewer_from_locator", lambda self, args: viewer)
        parser = _build_parser()
        ns = parser.parse_args(["job", "run-list", "--job-config", "foo.yaml"])

        asyncio.run(JobCommand().arun(ns))

        out = capsys.readouterr().out
        assert "run-1" in out
        assert "full" in out
        assert "org/ds" in out

    def test_run_status_prints_jobs(self, monkeypatch, capsys):
        from rock.sdk.job.meta import RunJobStatus, RunMeta

        viewer = MagicMock()

        class FakeRepo:
            def __init__(self, _viewer):
                pass

            def get(self, run_id):
                return RunMeta(run_id=run_id, mode="multi", status="partial", total_tasks=1, pending_tasks=0)

            def get_run_job_statuses(self, run_id):
                return [RunJobStatus(task_id="t1", job_name="j1", status="completed", score=1.0)]

        monkeypatch.setattr(JobCommand, "_build_viewer_from_locator", lambda self, args: viewer)
        monkeypatch.setattr("rock.sdk.job.run_meta.RunMetaRepository", FakeRepo)
        parser = _build_parser()
        ns = parser.parse_args(["job", "run-status", "--run-id", "run-1", "--job-config", "foo.yaml", "--jobs"])

        asyncio.run(JobCommand().arun(ns))

        out = capsys.readouterr().out
        assert "Run: run-1" in out
        assert "t1" in out
        assert "completed" in out
