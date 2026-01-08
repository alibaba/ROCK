import functools
import logging

from rock.actions import ResponseStatus, RockResponse

logger = logging.getLogger(__name__)


def handle_exceptions(error_message: str = "error occurred"):
    """Exception handling decorator

    Args:
        error_message: Default error message to return

    Returns:
        Decorator function
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
                return RockResponse(status=ResponseStatus.FAILED, message=error_message, error=str(e), result=None)

        return wrapper

    return decorator
