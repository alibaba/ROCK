import warnings

import pytest

from rock import RockException, codes
from rock.sdk.common.exceptions import (
    BadRequestRockError,
    InternalServerRockError,
    raise_for_envelope_or_result,
)


class TestRockException:
    """Test cases for the RockException class."""

    def test_rock_exception_basic_creation(self):
        """Test basic creation of RockException with message only."""
        message = "Test error message"
        exception = RockException(message)

        assert str(exception) == message
        assert exception.code is None
        assert isinstance(exception, Exception)

    def test_rock_exception_with_code(self):
        """Test RockException creation with both message and code."""
        message = "Test error with code"
        code = codes.BAD_REQUEST
        exception = RockException(message, code)

        assert str(exception) == message
        assert exception.code == code
        assert exception.code == 4000
        assert exception.code.phrase == "Bad Request"


class TestRaiseForEnvelopeOrResult:
    """Pin the contract of raise_for_envelope_or_result — the SDK helper that
    bridges the new envelope ``code`` path and the legacy ``result.code``
    payload during the migration window."""

    def test_prefers_envelope_code_over_result_code(self):
        """Envelope code wins. Result is ignored — no DeprecationWarning."""
        response = {
            "status": "Failed",
            "code": int(codes.BAD_REQUEST),
            "error": "envelope says bad request",
            "result": {"code": int(codes.INTERNAL_SERVER_ERROR), "failure_reason": "stale"},
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(BadRequestRockError, match="envelope says bad request"):
                raise_for_envelope_or_result(response, "Failed to start", "fallback")

    def test_falls_back_to_result_code_with_deprecation_warning(self):
        """No envelope code -> use result.code, but warn so callers upgrade."""
        response = {
            "status": "Failed",
            "result": {"code": int(codes.INTERNAL_SERVER_ERROR), "failure_reason": "legacy"},
        }
        with pytest.warns(DeprecationWarning, match="envelope `code` field"):
            with pytest.raises(InternalServerRockError, match="legacy"):
                raise_for_envelope_or_result(response, "Failed to start", "fallback")

    def test_generic_exception_when_neither_code_present(self):
        """No envelope code and no result -> generic Exception."""
        response = {"status": "Failed", "error": "anything"}
        with pytest.raises(Exception, match="fallback"):
            raise_for_envelope_or_result(response, "Failed to start", "fallback")

    def test_non_dict_result_skips_legacy_path(self):
        """When result is a non-dict (e.g. a string from a bare RockResponse
        endpoint), the legacy SandboxResponse parse must be skipped to avoid
        a TypeError crash."""
        response = {"status": "Failed", "error": "oops", "result": "some string"}
        with pytest.raises(Exception, match="fallback"):
            raise_for_envelope_or_result(response, "Failed to start", "fallback")
