"""Parameter validation utilities for API endpoints."""

from rock.actions import ResponseStatus, RockResponse


def validate_required_str(value: str | None, param_name: str) -> RockResponse | None:
    """Validate that a required string parameter is not None, empty, or whitespace-only.

    Returns a failed RockResponse on validation failure, or None on success. Returning
    (instead of raising) lets endpoints typed as ``RockResponse[T]`` early-return without
    tripping FastAPI's response_model validation on the error path — a raised exception
    routed through ``handle_exceptions`` would produce a ``RockResponse[SandboxResponse]``
    that mismatches the declared ``T``.

    Usage:
        if err := validate_required_str(sandbox_id, "sandbox_id"):
            return err
    """
    if value is None or not value.strip():
        return RockResponse(
            status=ResponseStatus.FAILED,
            error=f"{param_name} is required and must be a non-empty string",
            result=None,
        )
    return None
