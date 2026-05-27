from rock.actions import ResponseStatus
from rock.common.validation import validate_required_str


def test_validate_required_str_with_valid_value_returns_none():
    assert validate_required_str("sandbox-123", "sandbox_id") is None


def test_validate_required_str_with_padded_value_returns_none():
    assert validate_required_str("  sandbox-123  ", "sandbox_id") is None


def test_validate_required_str_none_returns_failed_response():
    resp = validate_required_str(None, "sandbox_id")
    assert resp is not None
    assert resp.status == ResponseStatus.FAILED
    assert "sandbox_id is required" in resp.error
    assert resp.result is None


def test_validate_required_str_empty_returns_failed_response():
    resp = validate_required_str("", "sandbox_id")
    assert resp is not None
    assert resp.status == ResponseStatus.FAILED
    assert "sandbox_id is required" in resp.error
    assert resp.result is None


def test_validate_required_str_whitespace_only_returns_failed_response():
    resp = validate_required_str("   ", "sandbox_id")
    assert resp is not None
    assert resp.status == ResponseStatus.FAILED
    assert "sandbox_id is required" in resp.error


def test_validate_required_str_uses_param_name_in_message():
    resp = validate_required_str("", "image")
    assert resp is not None
    assert "image is required" in resp.error
