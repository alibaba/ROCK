"""Unit tests for rock.cli.command.job.JobCommand.

All tests in this file are fast: no Docker, Ray, or network. We drive the
sub-parser end-to-end with argparse so mutual-exclusion and error messages
match what users see at the terminal.
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from rock.cli.command.job import JobCommand


def _build_parser() -> argparse.ArgumentParser:
    """Build a top-level parser with `job` subcommand wired in, same as the CLI."""
    top = argparse.ArgumentParser(prog="rock")
    subparsers = top.add_subparsers(dest="command")
    asyncio.run(JobCommand.add_parser_to(subparsers))
    return top


def test_parser_builds():
    """Smoke: the parser builds without error and exposes --job_config / --script."""
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--job_config", "foo.yaml"])
    assert ns.command == "job"
    assert ns.job_command == "run"
    assert ns.job_config == "foo.yaml"
    assert ns.script is None
    assert ns.script_content is None


def test_top_level_config_does_not_collide_with_job_config():
    """Regression: `rock --config cli.ini job run --job_config foo.yaml` keeps
    both values separate — the top-level --config (INI loader) and the sub-parser's
    --job_config (YAML) must not overwrite each other.
    """
    top = argparse.ArgumentParser(prog="rock")
    top.add_argument("--config", help="top-level CLI config (INI)")
    subparsers = top.add_subparsers(dest="command")
    asyncio.run(JobCommand.add_parser_to(subparsers))

    ns = top.parse_args(["--config", "cli.ini", "job", "run", "--job_config", "bash.yaml"])
    assert ns.config == "cli.ini"
    assert ns.job_config == "bash.yaml"


def test_job_config_hyphen_alias():
    """Both --job_config and --job-config should work (argparse accepts the alias)."""
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--job-config", "foo.yaml"])
    assert ns.job_config == "foo.yaml"


class TestFailHelper:
    def test_fail_exits_with_code_2_and_usage(self, capsys):
        """_fail() must print usage + msg and exit code 2 (argparse convention)."""
        from rock.cli.command.job import _fail

        parser = argparse.ArgumentParser(prog="rock job run")
        parser.add_argument("--config")

        with pytest.raises(SystemExit) as excinfo:
            _fail(parser, "boom")

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "usage:" in err
        assert "boom" in err
        assert "rock job run --help" in err  # always appended

    def test_fail_includes_hint_when_given(self, capsys):
        from rock.cli.command.job import _fail

        parser = argparse.ArgumentParser(prog="rock job run")

        with pytest.raises(SystemExit):
            _fail(parser, "boom", hint="try this: X")

        err = capsys.readouterr().err
        assert "boom" in err
        assert "try this: X" in err

    def test_fail_no_hint_still_appends_help_pointer(self, capsys):
        from rock.cli.command.job import _fail

        parser = argparse.ArgumentParser(prog="rock job run")

        with pytest.raises(SystemExit):
            _fail(parser, "boom")

        err = capsys.readouterr().err
        assert "rock job run --help" in err


class TestRunParserStash:
    def test_run_parser_stashed_on_class_after_add_parser_to(self):
        """After add_parser_to runs, JobCommand._run_parser must point to the 'run' sub-parser."""
        # Reset to isolate from other tests
        JobCommand._run_parser = None

        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))

        assert JobCommand._run_parser is not None
        assert isinstance(JobCommand._run_parser, argparse.ArgumentParser)
        # Sanity: it is the parser that knows about --config (stored as job_config)
        actions = {a.dest for a in JobCommand._run_parser._actions}
        assert "job_config" in actions
        assert "script" in actions


class TestJobRunValidation:
    @pytest.fixture(autouse=True)
    def _parser(self):
        """Rebuild the parser for each test so _run_parser is populated and fresh."""
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        self.top = top
        yield

    def _run(self, argv):
        """Parse argv and invoke JobCommand.arun; SystemExit bubbles up."""
        ns = self.top.parse_args(argv)
        cmd = JobCommand()
        asyncio.run(cmd.arun(ns))

    def test_missing_both_config_and_script_errors(self, capsys):
        """With neither --config nor --script*, must exit 2 with helpful message."""
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run"])

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "Missing job definition" in err
        assert "--job_config job.yaml" in err  # example in hint
        assert "--script" in err
        assert "rock job run --help" in err

    def test_config_and_script_mutually_exclusive(self, capsys, tmp_path):
        """--job_config together with --script must error with mutex hint."""
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text("script_path: ./run.sh\n")

        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--job_config", str(yaml_path), "--script", "run.sh"])

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err
        assert "YAML mode" in err
        assert "flags mode" in err

    def test_config_and_script_content_mutually_exclusive(self, capsys, tmp_path):
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text("script_path: ./run.sh\n")

        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--job_config", str(yaml_path), "--script-content", "echo hi"])

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_script_and_script_content_mutually_exclusive(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--script", "run.sh", "--script-content", "echo hi"])

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--script and --script-content are mutually exclusive" in err

    def test_type_harbor_requires_config(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._run(["job", "run", "--type", "harbor", "--script", "run.sh"])

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--type harbor requires --job_config" in err
        assert "cannot be expressed purely via CLI flags" in err

    def test_type_default_is_none(self):
        """--type should default to None; 'bash' is implied only when mode is flags."""
        ns = self.top.parse_args(["job", "run", "--script", "run.sh"])
        assert ns.type is None


class TestConfigFromFlags:
    def _args(self, **overrides):
        """Build an argparse.Namespace matching the run sub-parser defaults, then overlay overrides."""
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        argv = ["job", "run", "--script", "dummy.sh"]
        ns = top.parse_args(argv)
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def test_inline_script_content_with_env_and_uploads(self):
        ns = self._args(
            script=None,
            script_content="echo hi",
            image="python:3.11",
            memory="8g",
            cpus=4.0,
            env=["FOO=bar", "BAZ=qux"],
            local_path="/tmp/workspace",
            target_path="/root/job",
            timeout=1800,
        )
        config = JobCommand()._config_from_flags(ns)

        from rock.sdk.job.config import BashJobConfig

        assert isinstance(config, BashJobConfig)
        assert config.script == "echo hi"
        assert config.script_path is None
        assert config.timeout == 1800
        env = config.environment
        assert env.image == "python:3.11"
        assert env.memory == "8g"
        assert env.cpus == 4.0
        assert env.env == {"FOO": "bar", "BAZ": "qux"}
        assert env.uploads == [("/tmp/workspace", "/root/job")]

    def test_script_path_mode_no_env_no_uploads(self):
        ns = self._args(script="run.sh", script_content=None, env=None, local_path=None)
        config = JobCommand()._config_from_flags(ns)

        assert config.script_path == "run.sh"
        assert config.script is None
        assert config.environment.env == {}
        assert config.environment.uploads == []


class TestConfigFromYaml:
    def _setup(self):
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        return top

    def _make_args(self, top, argv):
        return top.parse_args(argv)

    def test_loads_bash_yaml_autodetected(self, tmp_path):
        top = self._setup()
        yaml_path = tmp_path / "bash.yaml"
        yaml_path.write_text("script_path: ./run.sh\ntimeout: 1800\nenvironment:\n  image: python:3.11\n")
        ns = self._make_args(top, ["job", "run", "--job_config", str(yaml_path)])
        config = JobCommand()._config_from_yaml(JobCommand._run_parser, ns)

        from rock.sdk.job.config import BashJobConfig

        assert isinstance(config, BashJobConfig)
        assert config.script_path == "./run.sh"
        assert config.timeout == 1800
        assert config.environment.image == "python:3.11"

    def test_missing_file_errors_via_parser(self, tmp_path, capsys):
        top = self._setup()
        missing = tmp_path / "nope.yaml"
        ns = self._make_args(top, ["job", "run", "--job_config", str(missing)])

        with pytest.raises(SystemExit) as excinfo:
            JobCommand()._config_from_yaml(JobCommand._run_parser, ns)

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--job_config path does not exist" in err

    def test_invalid_yaml_surfaces_from_yaml_error(self, tmp_path, capsys):
        top = self._setup()
        yaml_path = tmp_path / "weird.yaml"
        yaml_path.write_text("totally_unknown_field: 1\n")
        ns = self._make_args(top, ["job", "run", "--job_config", str(yaml_path)])

        with pytest.raises(SystemExit) as excinfo:
            JobCommand()._config_from_yaml(JobCommand._run_parser, ns)

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "Failed to load --job_config" in err
        assert "YAML does not match any known job type" in err

    def test_type_bash_matches_yaml(self, tmp_path):
        top = self._setup()
        yaml_path = tmp_path / "bash.yaml"
        yaml_path.write_text("script_path: ./run.sh\n")
        ns = self._make_args(top, ["job", "run", "--type", "bash", "--job_config", str(yaml_path)])
        config = JobCommand()._config_from_yaml(JobCommand._run_parser, ns)

        from rock.sdk.job.config import BashJobConfig

        assert isinstance(config, BashJobConfig)

    def test_type_harbor_mismatch_against_bash_yaml(self, tmp_path, capsys):
        top = self._setup()
        yaml_path = tmp_path / "bash.yaml"
        yaml_path.write_text("script_path: ./run.sh\n")
        ns = self._make_args(top, ["job", "run", "--type", "harbor", "--job_config", str(yaml_path)])

        with pytest.raises(SystemExit) as excinfo:
            JobCommand()._config_from_yaml(JobCommand._run_parser, ns)

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--type harbor does not match YAML (detected as bash)" in err


class TestApplyOverrides:
    def _setup(self):
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        return top

    def test_override_image_memory_cpus_on_bash_config(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        top = self._setup()
        config = BashJobConfig(
            script="echo hi",
            environment=RockEnvironmentConfig(image="python:3.10", memory="2g", cpus=1),
            timeout=7200,
        )
        ns = top.parse_args(
            [
                "job",
                "run",
                "--job_config",
                "unused.yaml",
                "--image",
                "python:3.12",
                "--memory",
                "16g",
                "--cpus",
                "8",
                "--timeout",
                "900",
            ]
        )
        JobCommand()._apply_overrides(config, ns)

        assert config.environment.image == "python:3.12"
        assert config.environment.memory == "16g"
        assert config.environment.cpus == 8.0
        assert config.timeout == 900

    def test_env_overrides_append_and_overwrite(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        top = self._setup()
        config = BashJobConfig(
            script="echo hi",
            environment=RockEnvironmentConfig(env={"FOO": "old", "KEEP": "1"}),
        )
        ns = top.parse_args(
            [
                "job",
                "run",
                "--job_config",
                "unused.yaml",
                "--env",
                "FOO=new",
                "--env",
                "NEW=2",
            ]
        )
        JobCommand()._apply_overrides(config, ns)

        assert config.environment.env == {"FOO": "new", "KEEP": "1", "NEW": "2"}

    def test_uploads_appended(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        top = self._setup()
        config = BashJobConfig(
            script="echo hi",
            environment=RockEnvironmentConfig(uploads=[("/a", "/b")]),
        )
        ns = top.parse_args(
            [
                "job",
                "run",
                "--job_config",
                "unused.yaml",
                "--local-path",
                "/src",
                "--target-path",
                "/dst",
            ]
        )
        JobCommand()._apply_overrides(config, ns)

        assert config.environment.uploads == [("/a", "/b"), ("/src", "/dst")]

    def test_parser_timeout_default_is_none(self):
        """--timeout should default to None so YAML timeout is not unconditionally overridden."""
        top = self._setup()
        ns = top.parse_args(["job", "run", "--script", "run.sh"])
        assert ns.timeout is None

    def test_parser_description_mentions_two_modes(self):
        self._setup()  # populates JobCommand._run_parser
        desc = JobCommand._run_parser.description or ""
        assert "YAML mode" in desc
        assert "flags mode" in desc

    def test_no_overrides_leaves_config_untouched(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        top = self._setup()
        config = BashJobConfig(
            script="echo hi",
            environment=RockEnvironmentConfig(image="python:3.10", env={"X": "1"}),
            timeout=1234,
        )
        ns = top.parse_args(["job", "run", "--job_config", "unused.yaml"])
        # Force timeout to None so this test is robust regardless of the parser's
        # current default (Task 11 flips it to None permanently).
        ns.timeout = None
        JobCommand()._apply_overrides(config, ns)

        assert config.environment.image == "python:3.10"
        assert config.environment.env == {"X": "1"}
        assert config.timeout == 1234


class TestJobRunEndToEnd:
    """End-to-end-ish tests for _job_run, mocking only Job.run."""

    def _setup(self):
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        return top

    def test_flags_mode_builds_bash_config_and_runs_job(self, monkeypatch):
        """A --script invocation should produce a BashJobConfig and call Job(config).run()."""
        from unittest.mock import MagicMock

        from rock.sdk.job.config import BashJobConfig

        captured = {}

        class FakeJob:
            def __init__(self, cfg):
                captured["cfg"] = cfg

            async def run(self):
                result = MagicMock()
                result.status = "COMPLETED"
                result.trial_results = []
                return result

        monkeypatch.setattr("rock.sdk.job.Job", FakeJob)

        top = self._setup()
        ns = top.parse_args(
            [
                "job",
                "run",
                "--script",
                "run.sh",
                "--image",
                "python:3.11",
                "--env",
                "A=1",
            ]
        )
        asyncio.run(JobCommand().arun(ns))

        cfg = captured["cfg"]
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script_path == "run.sh"
        assert cfg.environment.image == "python:3.11"
        assert cfg.environment.env == {"A": "1"}

    def test_yaml_mode_with_override_image(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        from rock.sdk.job.config import BashJobConfig

        yaml_path = tmp_path / "bash.yaml"
        yaml_path.write_text("script_path: ./run.sh\nenvironment:\n  image: python:3.10\n")

        captured = {}

        class FakeJob:
            def __init__(self, cfg):
                captured["cfg"] = cfg

            async def run(self):
                r = MagicMock()
                r.status = "COMPLETED"
                r.trial_results = []
                return r

        monkeypatch.setattr("rock.sdk.job.Job", FakeJob)

        top = self._setup()
        ns = top.parse_args(
            [
                "job",
                "run",
                "--job_config",
                str(yaml_path),
                "--image",
                "python:3.12",
            ]
        )
        asyncio.run(JobCommand().arun(ns))

        cfg = captured["cfg"]
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script_path == "./run.sh"
        assert cfg.environment.image == "python:3.12"  # override applied


class TestYamlSourceOfTruth:
    """Regression: YAML-loaded fields (base_url, cluster) must survive when the
    user doesn't pass the corresponding CLI flag, even though main.py would
    otherwise backfill args.base_url from the ~/.rock/config.ini default.
    """

    def _setup(self):
        JobCommand._run_parser = None
        top = argparse.ArgumentParser(prog="rock")
        # Mirror the top-level flags declared in rock/cli/main.py so we can
        # drive load_config_from_file() as main.py does.
        top.add_argument("--config")
        top.add_argument("--base-url")
        top.add_argument("--auth-token")
        top.add_argument("--cluster")
        top.add_argument("--extra-header", action="append", dest="extra_headers_list")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))
        return top

    def test_load_config_from_file_skips_backfill_for_job(self, tmp_path, monkeypatch):
        """load_config_from_file must NOT backfill base_url/cluster/auth_token
        when the command is `job` — the YAML is the source of truth.
        """
        from rock.cli import main as main_mod

        top = self._setup()
        ns = top.parse_args(["job", "run", "--job_config", "unused.yaml"])
        # Pretend the INI file would return http://ini.example.com as base_url
        fake_cli_config = type(
            "CLI", (), {"base_url": "http://ini.example.com", "extra_headers": {"cluster": "ini-cluster"}}
        )()
        monkeypatch.setattr(
            main_mod, "ConfigManager", lambda _path: type("M", (), {"get_config": lambda self: fake_cli_config})()
        )

        main_mod.load_config_from_file(ns)

        # Backfill should have been skipped for `job`
        assert ns.base_url is None
        assert ns.cluster is None
        assert ns.auth_token is None

    def test_load_config_from_file_backfills_for_non_job(self, tmp_path, monkeypatch):
        """Non-job commands still get the backfill behavior."""
        from rock.cli import main as main_mod

        # Build a parser with a non-job subcommand
        top = argparse.ArgumentParser(prog="rock")
        top.add_argument("--config")
        top.add_argument("--base-url")
        top.add_argument("--auth-token")
        top.add_argument("--cluster")
        top.add_argument("--extra-header", action="append", dest="extra_headers_list")
        subparsers = top.add_subparsers(dest="command")
        subparsers.add_parser("sandbox")
        ns = top.parse_args(["sandbox"])

        fake_cli_config = type(
            "CLI", (), {"base_url": "http://ini.example.com", "extra_headers": {"cluster": "ini-cluster"}}
        )()
        monkeypatch.setattr(
            main_mod, "ConfigManager", lambda _path: type("M", (), {"get_config": lambda self: fake_cli_config})()
        )

        main_mod.load_config_from_file(ns)

        assert ns.base_url == "http://ini.example.com"
        assert ns.cluster == "ini-cluster"

    def test_yaml_base_url_overridden_when_user_passes_flag(self, tmp_path, monkeypatch):
        """When user explicitly passes --base-url, it should override YAML."""
        from unittest.mock import MagicMock

        yaml_path = tmp_path / "bash.yaml"
        yaml_path.write_text("script_path: ./run.sh\nenvironment:\n  base_url: http://xrl.alibaba-inc.com\n")

        captured = {}

        class FakeJob:
            def __init__(self, cfg):
                captured["cfg"] = cfg

            async def run(self):
                r = MagicMock()
                r.status = "COMPLETED"
                r.trial_results = []
                return r

        monkeypatch.setattr("rock.sdk.job.Job", FakeJob)

        top = self._setup()
        ns = top.parse_args(
            [
                "job",
                "run",
                "--job_config",
                str(yaml_path),
                "--base-url",
                "http://explicit.example.com",
            ]
        )
        asyncio.run(JobCommand().arun(ns))

        cfg = captured["cfg"]
        assert cfg.environment.base_url == "http://explicit.example.com"


class TestArun:
    """Tests for JobCommand.arun dispatch (not _job_run)."""

    def test_unknown_job_command_logs_error(self):
        from unittest.mock import patch

        ns = argparse.Namespace(job_command="weird")
        with patch("rock.cli.command.job.logger") as mock_logger:
            asyncio.run(JobCommand().arun(ns))
            mock_logger.error.assert_called()


class TestHelpOutput:
    def test_help_output_mentions_both_modes(self, capsys):
        top = argparse.ArgumentParser(prog="rock")
        subparsers = top.add_subparsers(dest="command")
        asyncio.run(JobCommand.add_parser_to(subparsers))

        with pytest.raises(SystemExit) as excinfo:
            top.parse_args(["job", "run", "--help"])

        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "YAML mode" in out
        assert "flags mode" in out
        assert "--job_config" in out
        assert "--script" in out
