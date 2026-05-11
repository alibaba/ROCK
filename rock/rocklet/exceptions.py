from typing import Any


class RockletException(Exception):
    """Any exception that is raised by Rocklet."""


class SessionNotInitializedError(RockletException, RuntimeError):
    """Raised if we try to run a command in a shell that is not initialized."""


class NonZeroExitCodeError(RockletException, RuntimeError):
    """Can be raised if we execute a command in the shell and it has a non-zero exit code."""


class BashIncorrectSyntaxError(RockletException, RuntimeError):
    """Before running a bash command, we check for syntax errors.
    This is the error message for those syntax errors.
    """

    def __init__(self, message: str, *, extra_info: dict[str, Any] = None):
        super().__init__(message)
        if extra_info is None:
            extra_info = {}
        self.extra_info = extra_info


class PowerShellError(RockletException, RuntimeError):
    """Raised when a PowerShell session encounters an error."""

    def __init__(self, message: str, *, extra_info: dict[str, Any] = None):
        super().__init__(message)
        if extra_info is None:
            extra_info = {}
        self.extra_info = extra_info


class PowerShellNotFoundError(RockletException, FileNotFoundError):
    """Raised when PowerShell executable is not found on the system."""
    pass


class CommandTimeoutError(RockletException, RuntimeError, TimeoutError):
    ...


class NoExitCodeError(RockletException, RuntimeError):
    ...


class SessionExistsError(RockletException, ValueError):
    ...


class SessionDoesNotExistError(RockletException, ValueError):
    ...


class DeploymentNotStartedError(RockletException, RuntimeError):
    def __init__(self, message="Deployment not started"):
        super().__init__(message)


class DeploymentStartupError(RockletException, RuntimeError):
    ...


class DockerPullError(DeploymentStartupError):
    ...
