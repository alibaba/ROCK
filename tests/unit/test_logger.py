import io
import logging
import re
from datetime import datetime

from rock import env_vars
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.metrics.billing import log_billing_info
from rock.logger import init_logger


def test_init_logger_iso8601_format():
    env_vars.ROCK_LOGGING_PATH = "/tmp/rock_logs"
    env_vars.ROCK_TIME_ZONE = "Asia/Shanghai"
    captured_output = io.StringIO()
    logger = init_logger("test_logger", "test_logger.log")
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = captured_output

    logger.info("Test ISO 8601 format message")

    log_output = captured_output.getvalue()

    # ISO 8601 regex: YYYY-MM-DDTHH:MM:SS.mmm+ZZ:ZZ
    iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00"

    assert re.search(iso8601_pattern, log_output), f"Log timestamp should be in ISO 8601 format, got: {log_output}"
    assert "Test ISO 8601 format message" in log_output

    timestamp_match = re.search(iso8601_pattern, log_output)
    if timestamp_match:
        timestamp_str = timestamp_match.group()
        parsed_time = datetime.fromisoformat(timestamp_str)
        assert parsed_time is not None

    logger.handlers.clear()


def test_billing_log():
    sandbox_info: SandboxInfo = {
        "sandbox_id": "test_sandbox_id",
        "user_id": "test_user_id",
        "experiment_id": "test_experiment_id",
        "namespace": "test_namespace",
    }
    env_vars.ROCK_LOGGING_PATH = "/tmp/rock_logs"
    env_vars.ROCK_TIME_ZONE = "Asia/Shanghai"
    logger = init_logger("billing_test", file_name="billing.log")
    captured_output = io.StringIO()
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = captured_output

    log_billing_info(logger, sandbox_info)
    log_output = captured_output.getvalue()

    # eg. 2026-01-21T20:00:20.358+08:00 INFO:billing.py:11 [billing] [] [] -- {...}
    log_pattern = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+\d{2}:\d{2}) ([A-Z]+):([^ ]+) \[([^\]]+)\] \[([^\]]*)\] \[([^\]]*)\] -- (.*)"

    assert re.search(log_pattern, log_output), f"Log format should match the pattern, got: {log_output}"

    assert "test_sandbox_id" in log_output
    assert "test_user_id" in log_output

    logger.handlers.clear()
