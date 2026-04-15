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
