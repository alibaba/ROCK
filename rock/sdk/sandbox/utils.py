from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import asyncio
import functools
import sys
import time
from typing import TYPE_CHECKING

from rock.utils import retry_async

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import RunModeType, Sandbox


def _get_caller_logger_name() -> str:
    """Get the caller's module name for logger naming."""
    frame = sys._getframe(1)  # Skip current frame (decorator internal frame)
    # Walk up the stack until we find a frame not from utils.py
    while frame:
        if frame.f_code.co_filename != __file__:
            return frame.f_globals.get("__name__", "unknown")
        frame = frame.f_back
    return __name__


def with_time_logging(operation_name: str):
    """Decorator to add timing and logging to functions.

    This decorator:
    - Logs operation start and completion with elapsed time
    - Captures and re-raises exceptions with context
    - Supports both sync and async functions

    Args:
        operation_name: Name of the operation for logging

    Example:
        @with_time_logging("Installing model service")
        async def install(self):
            ...

        @with_time_logging("Loading config")
        def load_config(self):
            ...
    """

    from rock.logger import init_logger

    logger = init_logger(_get_caller_logger_name())

    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()

            logger.debug(f"{operation_name} started")

            try:
                result = await func(*args, **kwargs)

                elapsed = time.time() - start_time

                logger.info(f"{operation_name} completed (elapsed: {elapsed:.2f}s)")

                return result

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"{operation_name} failed: {str(e)} (elapsed: {elapsed:.2f}s)",
                    exc_info=True,
                )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()

            logger.info(f"Starting {operation_name}")

            try:
                result = func(*args, **kwargs)

                elapsed = time.time() - start_time

                logger.info(f"{operation_name} completed (elapsed: {elapsed:.2f}s)")

                return result

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"{operation_name} failed: {str(e)} (elapsed: {elapsed:.2f}s)",
                    exc_info=True,
                )
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


@retry_async(max_attempts=3, delay_seconds=5.0, backoff=2.0)
async def arun_with_retry(
    sandbox: Sandbox,
    cmd: str,
    session: str,
    mode: RunModeType,
    wait_timeout: int = 300,
    wait_interval: int = 10,
    error_msg: str = "Command failed",
):
    result = await sandbox.arun(
        cmd=cmd, session=session, mode=mode, wait_timeout=wait_timeout, wait_interval=wait_interval
    )
    # If exit_code is not 0, raise an exception to trigger retry
    if result.exit_code != 0:
        raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")
    return result
