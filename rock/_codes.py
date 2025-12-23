from __future__ import annotations

from enum import IntEnum

__all__ = ["codes"]


class codes(IntEnum):
    """
    ROCK status codes enumeration.

    This class extends IntEnum to provide status codes with associated phrase descriptions.
    Each enum member has both an integer value and a phrase attribute for human-readable descriptions.

    The class also provides utility methods to categorize codes and retrieve phrases.
    """

    _ignore_ = ["phrase"]
    phrase: str = ""

    def __new__(cls, value: int, phrase: str = "") -> codes:
        """
        Create a new codes enum member with both value and phrase.

        Args:
            value: The integer status code value
            phrase: Human-readable description of the status

        Returns:
            A new codes enum member with the phrase attribute set
        """
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.phrase = phrase  # Add phrase as an instance attribute
        return obj

    def __str__(self) -> str:
        """Return string representation of the status code value."""
        return str(self.value)

    @classmethod
    def get_reason_phrase(cls, value: int) -> str:
        """
        Get the reason phrase for a given status code value.

        Args:
            value: The integer status code value to look up

        Returns:
            The reason phrase string, or empty string if code not found

        Example:
            >>> codes.get_reason_phrase(2000)
            'OK'
            >>> codes.get_reason_phrase(9999)
            ''
        """
        try:
            return codes(value).phrase
        except ValueError:
            return ""

    @classmethod
    def is_success(cls, value: int) -> bool:
        """
        Check if a status code indicates success (2xxx range).

        Args:
            value: The status code to check

        Returns:
            True if the code is in the 2000-2999 range, False otherwise
        """
        return 2000 <= value <= 2999

    @classmethod
    def is_client_error(cls, value: int) -> bool:
        """
        Check if a status code indicates a client error (4xxx range).

        Args:
            value: The status code to check

        Returns:
            True if the code is in the 4000-4999 range, False otherwise
        """
        return 4000 <= value <= 4999

    @classmethod
    def is_server_error(cls, value: int) -> bool:
        """
        Check if a status code indicates a server error (5xxx range).

        Args:
            value: The status code to check

        Returns:
            True if the code is in the 5000-5999 range, False otherwise
        """
        return 5000 <= value <= 5999

    @classmethod
    def is_command_error(cls, value: int) -> bool:
        """
        Check if a status code indicates a command error (6xxx range).

        Args:
            value: The status code to check

        Returns:
            True if the code is in the 6000-6999 range, False otherwise
        """
        return 6000 <= value <= 6999

    @classmethod
    def is_error(cls, value: int) -> bool:
        """
        Check if a status code indicates any kind of error (4xxx or 5xxx range).

        Args:
            value: The status code to check

        Returns:
            True if the code is in the 4000-5999 range, False otherwise
        """
        return 4000 <= value <= 6999

    OK = 2000, "OK"
    """
    Success codes (2xxx)
    """

    BAD_REQUEST = 4000, "Bad Request"
    """
    Client error codes (4xxx):

    These errors indicate issues with the client request,
    SDK will raise Exceptions for these errors.
    """

    INTERNAL_SERVER_ERROR = 5000, "Internal Server Error"
    """
    Server error codes (5xxx):

    These errors indicate issues on the server side,
    SDK will raise Exceptions for these errors.
    """

    COMMAND_ERROR = 6000, "Command Error"
    """
    Command/execution error codes (6xxx):

    These errors are related to command execution and should be handled by the model,
    SDK will NOT raise Exceptions for these errors.
    """


# Include lower-case styles for `requests` compatibility.
for code in codes:
    setattr(codes, code._name_.lower(), int(code))
