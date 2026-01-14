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
    return SandboxResponse(code=e.code, failure_reason=str(e))
