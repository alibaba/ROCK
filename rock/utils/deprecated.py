import warnings
from collections.abc import Callable
from functools import wraps
from typing import Any


def deprecated(reason: str = "") -> Callable:
    """
    Decorator to mark a function or class as deprecated.

    Args:
        reason: Optional reason for deprecation

    Returns:
        Decorated function or class
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(f"{func.__name__} is deprecated. {reason}", DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)

        return wrapper

    return decorator
