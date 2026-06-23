"""Multi-worker logging: handler append mode + one-time deploy truncation."""

import importlib

import pytest

import rock.logger as rock_logger
from rock import env_vars


@pytest.fixture(autouse=True)
def _clear_handler_cache():
    rock_logger.init_file_handler.cache_clear()
    yield
    rock_logger.init_file_handler.cache_clear()


def test_rock_logging_append_defaults_false(monkeypatch):
    monkeypatch.delenv("ROCK_LOGGING_APPEND", raising=False)
    import rock.env_vars as ev

    importlib.reload(ev)
    assert ev.ROCK_LOGGING_APPEND is False


def test_file_handler_defaults_to_truncate_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_APPEND", False, raising=False)
    handler = rock_logger.init_file_handler("trunc_default.log")
    assert handler is not None
    assert handler.mode == "w+"


def test_file_handler_append_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_APPEND", True, raising=False)
    handler = rock_logger.init_file_handler("append_mode.log")
    assert handler is not None
    assert handler.mode == "a"


def test_reset_log_file_truncates_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_FILE_NAME", "deploy.log", raising=False)
    p = tmp_path / "deploy.log"
    p.write_text("stale lines from previous deploy\n")
    rock_logger.reset_log_file()
    assert p.read_text() == ""


def test_reset_log_file_noop_without_path(monkeypatch):
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_PATH", None, raising=False)
    # must not raise when file logging is disabled (stdout mode)
    rock_logger.reset_log_file()
