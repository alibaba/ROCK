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
    """Smoke: the parser builds without error and exposes --config / --script."""
    parser = _build_parser()
    ns = parser.parse_args(["job", "run", "--config", "foo.yaml"])
    assert ns.command == "job"
    assert ns.job_command == "run"
    assert ns.config == "foo.yaml"
    assert ns.script is None
    assert ns.script_content is None


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
        # Sanity: it is the parser that knows about --config
        actions = {a.dest for a in JobCommand._run_parser._actions}
        assert "config" in actions
        assert "script" in actions
