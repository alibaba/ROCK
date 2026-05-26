"""Parameter validation utilities for API endpoints."""

from rock.sdk.common.exceptions import InvalidParameterRockError


def validate_required_str(value: str | None, param_name: str) -> None:
    """Validate that a required string parameter is not None, empty, or whitespace-only.

    Raises InvalidParameterRockError if validation fails.
    """
    if value is None or not value.strip():
        raise InvalidParameterRockError(f"{param_name} is required and must be a non-empty string")
