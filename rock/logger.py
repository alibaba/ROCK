import logging
import os
import sys
from functools import cache

from rock import env_vars
from rock.utils import sandbox_id_ctx_var, trace_id_ctx_var


# Define the formatter class at module level since it doesn't need configuration state
class StandardFormatter(logging.Formatter):
    """Custom log formatter with color support"""

    def __init__(self, *args, log_color_enable: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_color_enable = log_color_enable

    def formatTime(self, record):
        """Format time as ISO 8601 with milliseconds and timezone"""
        from datetime import datetime, timezone

        # Use local timezone
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone()
        # Format as ISO 8601: 2026-01-20T07:26:39.268+08:00
        return dt.isoformat(timespec="milliseconds")

    def format(self, record):
        # ANSI color codes for different log levels
        COLORS = {
            logging.DEBUG: "\033[36m",
            logging.INFO: "\033[32m",
            logging.WARNING: "\033[33m",
            logging.ERROR: "\033[31m",
            logging.CRITICAL: "\033[35m",
        }
        RESET = "\033[0m"

        # Get the color for the current log level
        log_color = COLORS.get(record.levelno, "")

        # Get sandbox_id from context variable
        sandbox_id = sandbox_id_ctx_var.get()

        # Get trace_id from context variable
        trace_id = trace_id_ctx_var.get()

        # Format basic elements manually
        level_str = record.levelname
        time_str = self.formatTime(record)
        file_str = f"{record.filename}:{record.lineno}"
        logger_str = record.name  # This will be the logger name like 'myapp.utils'

        # Build header part
        header_str = f"{time_str} {level_str}:{file_str} [{logger_str}] [{sandbox_id}] [{trace_id}] --"

        # Color the header part and keep message in default color
        if self.log_color_enable:
            return f"{log_color}{header_str}{RESET} {record.getMessage()}"
        return f"{header_str} {record.getMessage()}"


@cache
def init_file_handler(log_name: str):
    if env_vars.ROCK_LOGGING_PATH:
        # Create file handler
        log_file_path = os.path.join(env_vars.ROCK_LOGGING_PATH, log_name)

        # Ensure directory exists
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

        handler = logging.FileHandler(log_file_path, mode="w+", encoding="utf-8")
        handler.setFormatter(StandardFormatter(log_color_enable=False))
        return handler
    return None


def init_logger(name: str | None = None, file_name: str | None = None):
    """Initialize and return a logger instance with custom handler and formatter

    Args:
        name: Logger name, defaults to "rock"

    Returns:
        Configured logger instance
    """
    logger_name = name if name else "rock"
    logger = logging.getLogger(logger_name)

    # Only add handler if logger doesn't have one yet to avoid duplicates
    if not logger.handlers:
        # Determine if we should log to file based on ROCK_LOGGING_PATH
        # Only log to file if ROCK_LOGGING_PATH has been explicitly set by the user
        # (not just the default value), which means it should be in os.environ
        file_name = file_name if file_name else env_vars.ROCK_LOGGING_FILE_NAME
        if env_vars.ROCK_LOGGING_PATH and file_name:
            # Use file handler
            handler = init_file_handler(file_name)
        else:
            # Use stdout handler
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(StandardFormatter())

        # Apply logging level from environment variable
        log_level = env_vars.ROCK_LOGGING_LEVEL

        handler.setLevel(log_level)

        # Add the handler to the logger
        logger.addHandler(handler)
        logger.setLevel(log_level)

        logger.propagate = False

        # Configure urllib3 specifically if this is called for the first time with appropriate logger
        if logger_name in ["rock", "admin"] or logger_name.startswith("rock."):
            logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logger
