from rock import RockException, codes


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
