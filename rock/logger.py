import logging
import os
import sys
from datetime import datetime
from functools import cache
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from rock import env_vars
from rock.utils import sandbox_id_ctx_var, trace_id_ctx_var

_exception_traceback_enabled = True


def configure_logging(*, exception_traceback_enabled: bool) -> None:
    global _exception_traceback_enabled
    _exception_traceback_enabled = exception_traceback_enabled


def is_exception_traceback_enabled() -> bool:
    if env_vars.is_set("ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE"):
        return env_vars.ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE
    return _exception_traceback_enabled


# Define the formatter class at module level since it doesn't need configuration state
class StandardFormatter(logging.Formatter):
    """Custom log formatter with color support"""

    def __init__(self, *args, log_color_enable: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_color_enable = log_color_enable

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

        message = record.getMessage()
        if is_exception_traceback_enabled() and record.exc_info and record.exc_info[0] is not None:
            exception_class = record.exc_info[0]
            exception_type = f"{exception_class.__module__}.{exception_class.__qualname__}"
            message = f"{message.rstrip()} [exception_type={exception_type}]\n{self.formatException(record.exc_info)}"

        # Color the header part and keep message in default color
        if self.log_color_enable:
            return f"{log_color}{header_str}{RESET} {message}"
        return f"{header_str} {message}"


class TimezoneFormatter(StandardFormatter):
    def __init__(self, *args, tz_string="Asia/Shanghai", **kwargs):
        super().__init__(*args, **kwargs)
        self.tz = ZoneInfo(tz_string)

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="milliseconds")


class ProcessRotatingFileHandler(RotatingFileHandler):
    """Rotate a separate file per process, including after a handler is forked."""

    def __init__(self, log_file_path: str, *, max_bytes: int, backup_count: int):
        if max_bytes <= 0:
            raise ValueError("ROCK_LOGGING_MAX_BYTES must be greater than zero")
        if backup_count <= 0:
            raise ValueError("ROCK_LOGGING_BACKUP_COUNT must be greater than zero")
        self._log_file_path = os.path.abspath(log_file_path)
        self._pid = os.getpid()
        super().__init__(
            self._process_file_path(),
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def _process_file_path(self) -> str:
        stem, suffix = os.path.splitext(self._log_file_path)
        return f"{stem}_{self._pid}{suffix}"

    def emit(self, record: logging.LogRecord) -> None:
        pid = os.getpid()
        if pid != self._pid:
            # Fork copies cached handlers and module-level loggers. Rebind even
            # when the child never calls init_logger() again, before rollover.
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self._pid = pid
            self.baseFilename = self._process_file_path()
        super().emit(record)


@cache
def init_file_handler(log_name: str):
    if env_vars.ROCK_LOGGING_PATH:
        # Create file handler
        log_file_path = os.path.join(env_vars.ROCK_LOGGING_PATH, log_name)

        # Ensure directory exists
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

        if env_vars.ROCK_LOGGING_PER_PROCESS:
            handler = ProcessRotatingFileHandler(
                log_file_path,
                max_bytes=env_vars.ROCK_LOGGING_MAX_BYTES,
                backup_count=env_vars.ROCK_LOGGING_BACKUP_COUNT,
            )
        else:
            # Preserve shared append logging and legacy truncate-on-start mode
            # for services that have not enabled per-process rotation.
            mode = "a" if env_vars.ROCK_LOGGING_APPEND else "w+"
            handler = logging.FileHandler(log_file_path, mode=mode, encoding="utf-8")
        handler.setFormatter(TimezoneFormatter(log_color_enable=False, tz_string=env_vars.ROCK_TIME_ZONE))
        return handler
    return None


def reset_log_file(file_name: str | None = None) -> None:
    """Truncate the shared log file once (e.g. at deploy / master startup).

    Under multi-worker, all workers open the same file in append mode, so the
    one-time clear must happen here in the master BEFORE workers spawn — doing
    it in the FileHandler would make each worker truncate and race. No-op when
    file logging is disabled (stdout mode) or per-process rotation is enabled.
    Rotated process logs always append, including when a PID is reused.
    """
    if not env_vars.ROCK_LOGGING_PATH or env_vars.ROCK_LOGGING_PER_PROCESS:
        return
    file_name = file_name or env_vars.ROCK_LOGGING_FILE_NAME
    if not file_name:
        return
    log_file_path = os.path.join(env_vars.ROCK_LOGGING_PATH, file_name)
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    open(log_file_path, "w").close()


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
            handler.setFormatter(TimezoneFormatter(tz_string=env_vars.ROCK_TIME_ZONE))

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
