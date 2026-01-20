import io
import logging
import re
from datetime import datetime

from rock.logger import init_logger


def test_init_logger_iso8601_format():
    captured_output = io.StringIO()

    logger = init_logger("test_logger")

    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = captured_output

    logger.info("Test ISO 8601 format message")

    log_output = captured_output.getvalue()

    # ISO 8601 regex: YYYY-MM-DDTHH:MM:SS.mmm+ZZ:ZZ
    iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}"

    assert re.search(iso8601_pattern, log_output), f"Log timestamp should be in ISO 8601 format, got: {log_output}"

    assert "Test ISO 8601 format message" in log_output

    timestamp_match = re.search(iso8601_pattern, log_output)
    if timestamp_match:
        timestamp_str = timestamp_match.group()
        parsed_time = datetime.fromisoformat(timestamp_str)
        assert parsed_time is not None

    logger.handlers.clear()
