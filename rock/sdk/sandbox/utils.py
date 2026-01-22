from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import functools
import time
from typing import TYPE_CHECKING

from rock.utils import retry_async

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import RunModeType, Sandbox


def with_time_logging(operation_name: str):
    """Decorator to add timing and logging to async methods.

    This decorator:
    - Logs operation start and completion with elapsed time
    - Captures and re-raises exceptions with context
    - Provides consistent error handling across methods

    Args:
        operation_name: Name of the operation for logging
        log_start: Whether to log operation start (default: True)
        log_success: Whether to log successful completion (default: True)

    Example:
        @with_time_logging("Installing model service")
        async def install(self):
            ...
    """
    from rock.logger import init_logger

    logger = init_logger(__name__)

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            sandbox_id = getattr(self._sandbox, "sandbox_id", "unknown")
            start_time = time.time()

            logger.info(f"[{sandbox_id}] Starting {operation_name}")

            try:
                result = await func(self, *args, **kwargs)

                elapsed = time.time() - start_time

                logger.info(f"[{sandbox_id}] {operation_name} completed (elapsed: {elapsed:.2f}s)")

                return result

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"[{sandbox_id}] {operation_name} failed: {str(e)} (elapsed: {elapsed:.2f}s)",
                    exc_info=True,
                )
                raise

        return wrapper

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
