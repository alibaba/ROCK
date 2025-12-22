import rock
from rock.actions.response import RockResponse
from rock.utils.deprecated import deprecated


class RockException(Exception):
    _code: rock.codes = None

    def __init__(self, message, code: rock.codes = None):
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
    def __init__(self, message, code: rock.codes = rock.codes.BAD_REQUEST):
        super().__init__(message, code)


class InternalServerRockError(RockException):
    def __init__(self, message, code: rock.codes = rock.codes.INTERNAL_SERVER_ERROR):
        super().__init__(message, code)


class CommandRockError(RockException):
    def __init__(self, message, code: rock.codes = rock.codes.COMMAND_ERROR):
        super().__init__(message, code)


def raise_for_code(code: rock.codes, message: str):
    if code is None or rock.codes.is_success(code):
        return

    if rock.codes.is_client_error(code):
        raise BadRequestRockError(message)
    if rock.codes.is_server_error(code):
        raise InternalServerRockError(message)
    if rock.codes.is_command_error(code):
        raise CommandRockError(message)

    raise RockException(message, code=code)


def from_rock_exception(e: RockException) -> RockResponse:
    return RockResponse(code=e.code, failure_reason=str(e))
