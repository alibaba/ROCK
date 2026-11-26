import logging
import multiprocessing
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import rock.logger as logger_module
from rock import env_vars


@pytest.fixture
def rotation_logging(monkeypatch, tmp_path):
    monkeypatch.setenv("ROCK_LOGGING_PATH", str(tmp_path))
    monkeypatch.setattr(env_vars, "ROCK_LOGGING_PATH", str(tmp_path))
    monkeypatch.setenv("ROCK_LOGGING_PER_PROCESS", "true")
    monkeypatch.delenv("ROCK_LOGGING_MAX_BYTES", raising=False)
    monkeypatch.delenv("ROCK_LOGGING_BACKUP_COUNT", raising=False)
    logger_module.init_file_handler.cache_clear()
    handlers = set()

    def make_logger(file_name="rock_admin.log"):
        handler = logger_module.init_file_handler(file_name)
        handlers.add(handler)
        logger = logging.Logger("rotation-check", level=logging.INFO)
        logger.addHandler(handler)
        return logger, handler

    yield tmp_path, make_logger

    for handler in handlers:
        handler.close()
    logger_module.init_file_handler.cache_clear()


@pytest.mark.parametrize("file_name", ["rock_admin.log", "billing.log", "scheduler.log", "nested/rock.admin.log"])
@pytest.mark.parametrize("per_process", [None, "true"], ids=["default", "explicit"])
def test_process_logs_preserve_base_name_and_append_pid(rotation_logging, monkeypatch, file_name, per_process):
    if per_process is None:
        monkeypatch.delenv("ROCK_LOGGING_PER_PROCESS", raising=False)
    else:
        monkeypatch.setenv("ROCK_LOGGING_PER_PROCESS", per_process)
    directory, make_logger = rotation_logging
    original = directory / file_name
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("existing shared log\n")
    logger, handler = make_logger(file_name)

    logger.info("new process log")

    expected = original.with_name(f"{original.stem}_{os.getpid()}{original.suffix}")
    assert Path(handler.baseFilename) == expected
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 128 * 1024 * 1024
    assert handler.backupCount == 3
    assert original.read_text() == "existing shared log\n"
    assert expected.read_text().endswith("-- new process log\n")
    assert "\x1b" not in expected.read_text()


def test_process_log_reuses_handler_without_truncating_existing_data(rotation_logging):
    directory, make_logger = rotation_logging
    active = directory / f"rock_admin_{os.getpid()}.log"
    active.write_text("previous process lifetime\n")
    first, first_handler = make_logger()
    second, second_handler = make_logger()

    first.info("first logger")
    second.info("second logger")

    assert first_handler is second_handler
    assert active.read_text().startswith("previous process lifetime\n")
    assert active.read_text().count("-- first logger\n") == 1
    assert active.read_text().count("-- second logger\n") == 1


def test_process_rotation_limits_backups_and_keeps_latest_file_name(rotation_logging, monkeypatch):
    directory, make_logger = rotation_logging
    monkeypatch.setenv("ROCK_LOGGING_MAX_BYTES", "512")
    monkeypatch.setenv("ROCK_LOGGING_BACKUP_COUNT", "2")
    logger, handler = make_logger()

    for index in range(10):
        logger.info("event-%02d %s", index, "x" * 240)

    active = directory / f"rock_admin_{os.getpid()}.log"
    assert sorted(path.name for path in directory.iterdir()) == [
        active.name,
        active.name + ".1",
        active.name + ".2",
    ]
    assert "event-09" in active.read_text()
    assert "event-00" not in "".join(path.read_text() for path in directory.iterdir())
    assert all(path.stat().st_size <= 512 for path in directory.iterdir())
    assert Path(handler.baseFilename) == active


def test_reset_leaves_shared_and_pid_logs_intact_when_rotation_is_enabled(rotation_logging):
    directory, make_logger = rotation_logging
    shared = directory / "rock_admin.log"
    shared.write_text("shared log before migration\n")
    logger, _ = make_logger()
    logger.info("retain across startup reset")

    logger_module.reset_log_file("rock_admin.log")

    assert shared.read_text() == "shared log before migration\n"
    assert "retain across startup reset" in (directory / f"rock_admin_{os.getpid()}.log").read_text()


@pytest.mark.parametrize("append", [False, True])
def test_disabled_process_rotation_preserves_legacy_logging(rotation_logging, monkeypatch, append):
    directory, make_logger = rotation_logging
    monkeypatch.setenv("ROCK_LOGGING_PER_PROCESS", "false")
    monkeypatch.setenv("ROCK_LOGGING_APPEND", str(append).lower())
    active = directory / "rocklet.log"
    active.write_text("old content\n")
    logger, handler = make_logger("rocklet.log")

    logger.info("legacy log")

    assert type(handler) is logging.FileHandler
    assert ("old content" in active.read_text()) is append
    assert active.read_text().endswith("-- legacy log\n")
    logger_module.reset_log_file("rocklet.log")
    assert active.read_text() == ""


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ROCK_LOGGING_MAX_BYTES", "0"),
        ("ROCK_LOGGING_MAX_BYTES", "-1"),
        ("ROCK_LOGGING_BACKUP_COUNT", "0"),
        ("ROCK_LOGGING_BACKUP_COUNT", "-1"),
    ],
)
def test_rejects_unbounded_process_rotation(rotation_logging, monkeypatch, key, value):
    _, make_logger = rotation_logging
    monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match=key):
        make_logger()


def _write_spawned_log():
    logger = logger_module.init_logger("spawned-rotation-check", file_name="rock_admin.log")
    logger.info("spawned child")
    for handler in logger.handlers:
        handler.close()


def test_spawned_process_writes_its_own_file(rotation_logging):
    directory, make_logger = rotation_logging
    logger, _ = make_logger()
    logger.info("parent")
    process = multiprocessing.get_context("spawn").Process(target=_write_spawned_log)
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("spawned logging process did not exit")

    assert process.exitcode == 0
    parent_log = (directory / f"rock_admin_{os.getpid()}.log").read_text()
    child_log = (directory / f"rock_admin_{process.pid}.log").read_text()
    assert "-- parent\n" in parent_log
    assert "spawned child" not in parent_log
    assert "spawned child" in child_log


def _write_inherited_log(logger):
    logger.info("inherited child")


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires fork")
def test_forked_process_reopens_inherited_handler_before_writing(rotation_logging):
    directory, make_logger = rotation_logging
    logger, _ = make_logger()
    logger.info("parent before fork")
    process = multiprocessing.get_context("fork").Process(target=_write_inherited_log, args=(logger,))
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("forked logging process did not exit")
    logger.info("parent after fork")

    assert process.exitcode == 0
    parent_log = (directory / f"rock_admin_{os.getpid()}.log").read_text()
    child_log = (directory / f"rock_admin_{process.pid}.log").read_text()
    assert "parent before fork" in parent_log
    assert "parent after fork" in parent_log
    assert "inherited child" not in parent_log
    assert "inherited child" in child_log
