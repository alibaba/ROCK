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
    """Backward-compat: populate ``result.code`` for older SDKs."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return SandboxResponse(code=e.code, failure_reason=str(e))


def raise_for_envelope_or_result(response: dict, container_message: str, fallback_message: str) -> None:
    """Raise from envelope ``code``, fall back to legacy ``result.code``."""
    envelope_code = response.get("code")
    if envelope_code is not None:
        raise_for_code(envelope_code, f"{container_message}: {response}")
    result = response.get("result", None)
    if isinstance(result, dict):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            rock_response = SandboxResponse(**result)
        if rock_response.code is not None:
            warnings.warn(
                "Reading the error code from `result` is deprecated; upgrade the "
                "rock admin so the envelope `code` field is populated.",
                DeprecationWarning,
                stacklevel=2,
            )
            raise_for_code(rock_response.code, f"{container_message}: {response}")
    raise Exception(f"{fallback_message}: {response}")
