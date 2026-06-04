import warnings

from rock._codes import codes
from rock.actions import SandboxResponse
from rock.utils.deprecated import deprecated


class RockException(Exception):
    _code: codes = None

    def __init__(self, message, code: codes = None):
        super().__init__(message)
        self._code = code

    @property
    def code(self):
        return self._code


@deprecated("This exception is deprecated")
class InvalidParameterRockException(RockException):
    def __init__(self, message):
        super().__init__(message)


class BadRequestRockError(RockException):
    def __init__(self, message, code: codes = codes.BAD_REQUEST):
        super().__init__(message, code)


class InternalServerRockError(RockException):
    def __init__(self, message, code: codes = codes.INTERNAL_SERVER_ERROR):
        super().__init__(message, code)


class CommandRockError(RockException):
    def __init__(self, message, code: codes = codes.COMMAND_ERROR):
        super().__init__(message, code)


def raise_for_code(code: codes, message: str):
    if code is None or codes.is_success(code):
        return

    if codes.is_client_error(code):
        raise BadRequestRockError(message)
    if codes.is_server_error(code):
        raise InternalServerRockError(message)
    if codes.is_command_error(code):
        raise CommandRockError(message)

    raise RockException(message, code=code)


def from_rock_exception(e: RockException) -> SandboxResponse:
    """Legacy helper: build a ``SandboxResponse`` payload to stuff into
    ``RockResponse.result`` on error.

    Kept for backward-compat with SDKs that read the structured error from
    ``result.code`` / ``result.failure_reason``. New consumers should read
    ``code`` from the response envelope (``RockResponse.code``) instead;
    populating it on ``result`` will be removed once all SDK consumers have
    migrated.
    """
    return SandboxResponse(code=e.code, failure_reason=str(e))


def raise_for_envelope_or_result(response: dict, container_message: str, fallback_message: str) -> None:
    """Raise a typed ``RockException`` based on the failed response envelope.

    Prefers the envelope ``code`` field (the new contract). Falls back to the
    legacy ``result.code`` payload (``SandboxResponse``-shaped) for
    compatibility with older admin servers and emits a ``DeprecationWarning``
    so callers know to upgrade. Raises a generic ``Exception`` when neither
    is present.

    Args:
        response: Parsed JSON response body from the admin API.
        container_message: Message passed to ``raise_for_code`` describing the
            failed operation.
        fallback_message: Message for the generic ``Exception`` raised when
            no structured code can be recovered.
    """
    envelope_code = response.get("code")
    if envelope_code is not None:
        raise_for_code(envelope_code, f"{container_message}: {response}")
    result = response.get("result", None)
    if result is not None:
        warnings.warn(
            "Reading the error code from `result` is deprecated; upgrade the "
            "rock admin so the envelope `code` field is populated.",
            DeprecationWarning,
            stacklevel=2,
        )
        rock_response = SandboxResponse(**result)
        raise_for_code(rock_response.code, f"{container_message}: {response}")
    raise Exception(f"{fallback_message}: {response}")
