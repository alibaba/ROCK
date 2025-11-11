import asyncio
import functools
import logging
import random

logger = logging.getLogger(__name__)


def retry_async(
    max_attempts: int = 3,
    delay_seconds: float = 1.0,
    backoff: float = 1.0,
    jitter: bool = False,
    exceptions: tuple = (Exception,),
):
    def decorator(coro_func):
        @functools.wraps(coro_func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay_seconds
            for attempt in range(1, max_attempts + 1):
                try:
                    return await coro_func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    logger.warning(f"the {attempt}th attempt failed: {str(e)}", exc_info=e)

                    if attempt == max_attempts:
                        break

                    sleep_time = current_delay
                    if jitter:
                        sleep_time = random.uniform(0, current_delay * 2)

                    logger.info(f"will retry after {sleep_time} seconds")
                    await asyncio.sleep(sleep_time)

                    current_delay *= backoff

            logger.error(f"all {max_attempts} attempts failed", exc_info=last_exception)
            raise last_exception  # type: ignore

        return wrapper

    return decorator
