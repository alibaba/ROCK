import logging
import os
import sys

from rock import env_vars
from rock.utils import sandbox_id_ctx_var


# Define the formatter class at module level since it doesn't need configuration state
class StandardFormatter(logging.Formatter):
    """Custom log formatter with color support"""

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

        # Format basic elements manually
        level_str = record.levelname
        time_str = self.formatTime(record)
        file_str = f"{record.filename}:{record.lineno}"
        logger_str = record.name  # This will be the logger name like 'myapp.utils'

        # Build header part with or without sandbox_id
        if sandbox_id:
            header_str = f"{time_str} {level_str}:{file_str} [{logger_str}] [{sandbox_id}] --"
        else:
            header_str = f"{time_str} {level_str}:{file_str} [{logger_str}] -- "

        # Color the header part and keep message in default color
        return f"{log_color}{header_str}{RESET} {record.getMessage()}"


def _should_skip_stdout(logger_name: str) -> bool:
    """
    Determine if a logger should skip stdout output (only output to file if configured)

    Args:
        logger_name: The name of the logger

    Returns:
        True if logger should skip stdout, False otherwise
    """
    # Check if logger name matches specific patterns that should not output to stdout
    if logger_name.startswith("rock.actions.local") or logger_name.startswith("rocklet."):
        return True
    return False


def init_logger(name: str | None = None):
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
        # Priority 1: File logging (if configured, applies to ALL loggers)
        if env_vars.ROCK_LOGGING_PATH and env_vars.ROCK_LOGGING_FILE_NAME:
            log_file_path = os.path.join(env_vars.ROCK_LOGGING_PATH, env_vars.ROCK_LOGGING_FILE_NAME)
            # Ensure directory exists
            os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
            handler = logging.FileHandler(log_file_path)
        # Priority 2: Skip stdout for specific logger patterns (no output at all)
        elif _should_skip_stdout(logger_name):
            return logger
        # Priority 3: Default to stdout for other loggers
        else:
            handler = logging.StreamHandler(sys.stdout)

        handler.setFormatter(StandardFormatter())

        # Apply logging level from environment variable
        log_level = env_vars.ROCK_LOGGING_LEVEL

        handler.setLevel(log_level)

        # Add the handler to the logger
        logger.addHandler(handler)
        logger.setLevel(log_level)

        # Configure urllib3 specifically if this is called for the first time with appropriate logger
        if logger_name in ["rock", "admin"] or logger_name.startswith("rock."):
            logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logger
