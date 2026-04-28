import argparse
import logging

import pytest

from rock.cli.main import config_log


def _make_args(verbose=0, httpx_log_level=None):
    """Helper to create argparse.Namespace for config_log tests."""
    return argparse.Namespace(verbose=verbose, httpx_log_level=httpx_log_level)


@pytest.fixture(autouse=True)
def _reset_log_levels():
    """Reset logger levels before each test to avoid cross-test pollution."""
    # Save original levels
    loggers = ["rock", "rock.cli", "rock.sdk.job.executor", "httpx", "httpcore", "urllib3"]
    saved = {name: logging.getLogger(name).level for name in loggers}
    saved_handlers = {}
    for name in loggers:
        logger = logging.getLogger(name)
        saved_handlers[name] = [(h, h.level) for h in logger.handlers]

    yield

    # Restore original levels
    for name in loggers:
        logger = logging.getLogger(name)
        logger.setLevel(saved[name])
        for handler, orig_level in saved_handlers[name]:
            handler.setLevel(orig_level)


class TestConfigLogVerbosity:
    """Test -v count → unified log level mapping."""

    def test_default_error(self):
        config_log(_make_args(verbose=0))
        assert logging.getLogger("rock").level == logging.ERROR

    def test_v1_warning(self):
        config_log(_make_args(verbose=1))
        assert logging.getLogger("rock").level == logging.WARNING

    def test_v2_info(self):
        config_log(_make_args(verbose=2))
        assert logging.getLogger("rock").level == logging.INFO

    def test_v3_debug(self):
        config_log(_make_args(verbose=3))
        assert logging.getLogger("rock").level == logging.DEBUG

    def test_overflow_clamped_to_debug(self):
        config_log(_make_args(verbose=99))
        assert logging.getLogger("rock").level == logging.DEBUG


class TestConfigLogThirdParty:
    """Test third-party logger levels follow -v by default."""

    def test_third_party_follows_verbosity(self):
        config_log(_make_args(verbose=2))
        for name in ("httpx", "httpcore", "urllib3"):
            assert logging.getLogger(name).level == logging.INFO

    def test_third_party_default_error(self):
        config_log(_make_args(verbose=0))
        for name in ("httpx", "httpcore", "urllib3"):
            assert logging.getLogger(name).level == logging.ERROR


class TestConfigLogHttpxOverride:
    """Test --httpx-log-level overrides -v inference for third-party only."""

    def test_httpx_override_does_not_affect_rock(self):
        config_log(_make_args(verbose=0, httpx_log_level="WARNING"))
        assert logging.getLogger("rock").level == logging.ERROR
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_httpx_override_with_v(self):
        config_log(_make_args(verbose=2, httpx_log_level="WARNING"))
        assert logging.getLogger("rock").level == logging.INFO
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_httpx_log_level_none_uses_verbosity(self):
        config_log(_make_args(verbose=2, httpx_log_level=None))
        assert logging.getLogger("httpx").level == logging.INFO


class TestConfigLogRockHandler:
    """Test rock logger handler levels are also updated."""

    def test_rock_handler_level_updated(self):
        config_log(_make_args(verbose=3))
        for handler in logging.getLogger("rock").handlers:
            assert handler.level == logging.DEBUG


class TestConfigLogChildLogger:
    """Test rock.* child loggers with propagate=False are also updated."""

    def test_child_logger_level_updated(self):
        """Simulate init_logger behavior: child logger with own handler + propagate=False."""
        child = logging.getLogger("rock.cli")
        child.addHandler(logging.StreamHandler())
        child.setLevel(logging.INFO)
        child.propagate = False

        config_log(_make_args(verbose=0))
        assert child.level == logging.ERROR

    def test_child_handler_level_updated(self):
        """Child logger handler level should also be updated by config_log."""
        child = logging.getLogger("rock.cli")
        child.addHandler(logging.StreamHandler())
        child.setLevel(logging.INFO)
        child.propagate = False

        config_log(_make_args(verbose=3))
        for handler in child.handlers:
            assert handler.level == logging.DEBUG

    def test_deep_child_logger_updated(self):
        """Deeply nested rock.* logger (e.g. rock.sdk.job.executor) is also updated."""
        child = logging.getLogger("rock.sdk.job.executor")
        child.addHandler(logging.StreamHandler())
        child.setLevel(logging.INFO)
        child.propagate = False

        config_log(_make_args(verbose=2))
        assert child.level == logging.INFO

    def test_non_rock_logger_not_affected(self):
        """Non-rock child loggers should NOT be changed by config_log."""
        other = logging.getLogger("myapp.worker")
        other.addHandler(logging.StreamHandler())
        other.setLevel(logging.DEBUG)
        other.propagate = False

        config_log(_make_args(verbose=0))
        assert other.level == logging.DEBUG  # unchanged
