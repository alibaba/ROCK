import functools

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rock.actions import ResponseStatus, RockResponse
from rock.logger import init_logger
from rock.sdk.common.exceptions import RockException, from_rock_exception

logger = init_logger(__name__)


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map FastAPI's RequestValidationError to the project's RockResponse envelope.

    FastAPI registers a default handler for RequestValidationError that returns
    422 ``{"detail": [...]}``. That shape clashes with the rest of the API, where
    business failures come back as ``RockResponse(status=Failed, error=...)`` over
    HTTP 200. Registering this handler on the FastAPI app aligns Pydantic-driven
    validation errors with the same contract used by ``validate_required_str`` —
    callers see one shape regardless of where validation happened.
    """
    msg = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    logger.warning("request validation failed on %s: %s", request.url.path, msg)
    return JSONResponse(
        status_code=200,
        content=RockResponse(
            status=ResponseStatus.FAILED,
            message="invalid parameter",
            error=msg,
            result=None,
        ).model_dump(),
    )


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
            # Return JSONResponse directly on error so FastAPI skips response_model validation.
            # Otherwise endpoints declared as RockResponse[str] (e.g. /stop, /commit) would
            # 500 because the error payload is a SandboxResponse, not a str.
            try:
                return await func(*args, **kwargs)
            except RockException as e:
                logger.error(f"RockException in {func.__name__}: {str(e)}", exc_info=True)
                return JSONResponse(
                    status_code=200,
                    content=RockResponse(
                        status=ResponseStatus.FAILED,
                        message=error_message,
                        result=from_rock_exception(e),
                    ).model_dump(),
                )
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
                return JSONResponse(
                    status_code=200,
                    content=RockResponse(
                        status=ResponseStatus.FAILED,
                        message=error_message,
                        error=str(e),
                        result=None,
                    ).model_dump(),
                )

        return wrapper

    return decorator
