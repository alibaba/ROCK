import pytest

from rock.common.validation import validate_required_str
from rock.sdk.common.exceptions import BadRequestRockError, InvalidParameterRockError


def test_validate_required_str_with_valid_value():
    validate_required_str("sandbox-123", "sandbox_id")


def test_validate_required_str_with_padded_value():
    validate_required_str("  sandbox-123  ", "sandbox_id")


def test_validate_required_str_none_raises():
    with pytest.raises(InvalidParameterRockError, match="sandbox_id is required"):
        validate_required_str(None, "sandbox_id")


def test_validate_required_str_empty_raises():
    with pytest.raises(InvalidParameterRockError, match="sandbox_id is required"):
        validate_required_str("", "sandbox_id")


def test_validate_required_str_whitespace_only_raises():
    with pytest.raises(InvalidParameterRockError, match="sandbox_id is required"):
        validate_required_str("   ", "sandbox_id")


def test_validate_required_str_uses_param_name_in_message():
    with pytest.raises(InvalidParameterRockError, match="image is required"):
        validate_required_str("", "image")


def test_invalid_parameter_is_bad_request_subclass():
    """Existing `except BadRequestRockError` handlers must keep catching the new error."""
    with pytest.raises(BadRequestRockError):
        validate_required_str("", "image")
