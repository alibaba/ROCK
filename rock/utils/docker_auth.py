import logging
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from rock import env_vars

logger = logging.getLogger(__name__)



class TempDockerAuthError(Exception):
    """Docker temporary authentication scheme exception types"""
    pass


class TempDockerAuth:
    """Docker temp auth manager

    Use a separate temporary configuration directory for Docker login/pull to ensure:
    1. Credentials do not pollute the user's home directory's `~/.docker/config.json` file.
    2. Credentials are completely isolated within each sandbox.
    3. Automatic cleanup; credentials are not persistent.
    """

    def __init__(self, base_dir: str | None = None):
        """init

        Args:
            base_dir: The parent directory of the temporary directory is the system temporary directory (/tmp) by default.
        """
        self._base_dir = base_dir or env_vars.ROCK_DOCKER_TEMP_AUTH_DIR
        self._temp_dir: Path | None = None

    @property
    def temp_dir(self) -> Path | None:
        """Get the temporary directory path"""
        return self._temp_dir

    @property
    def config_path(self) -> Path | None:
        """Get the temporary config.json path"""
        if self._temp_dir:
            return self._temp_dir / "config.json"
        return None

    def create(self) -> Path:
        """Create a temporary configuration directory

        Returns:
            Temporary directory path
        """
        prefix = "rock_docker_auth_"
        if self._base_dir:
            Path(self._base_dir).mkdir(parents=True, exist_ok=True)
            self._temp_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=self._base_dir))
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix=prefix))

        logger.debug(f"Created temp docker config dir: {self._temp_dir}")
        return self._temp_dir

    def cleanup(self) -> None:
        """Clean up the temporary configuration directory and return a success/failure status."""
        if self._temp_dir:
            if self._temp_dir.exists():
                try:
                    shutil.rmtree(self._temp_dir, ignore_errors=True)
                    logger.debug(f"Cleaned up temp docker config dir: {self._temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp docker config dir: {e}")
            """Even if deletion fails, it indicates "this directory is no longer managed," avoiding duplicate operations and status confusion."""
            self._temp_dir = None

    def login(self, registry: str, username: str, password: str, timeout: int = 30) -> None:
        """Login to a Docker registry

        Args:
            registry: Docker registry URL (e.g. registry.example.com)
            username: Registry username
            password: Registry password
            timeout: Command timeout in seconds

        Returns:
            Command output as string on success

        Raises:
            TempDockerAuthError: If login fails
        """
        if not self._temp_dir:
            raise TempDockerAuthError("Temp dir not created. Call create() first.")
        try:
            result = subprocess.run(
                [
                    "docker",
                    "--config", str(self._temp_dir),
                    "login",
                    registry,
                    "-u", username,
                    "--password-stdin"
                ],
                input=password,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode != 0:
                error_msg = f"Docker login failed: {result.stderr.strip()}"
                logger.error(error_msg)
                raise TempDockerAuthError(error_msg)

            logger.info(f"Successfully logged in to {registry} using temp config")
        except subprocess.TimeoutExpired:
            raise TempDockerAuthError(f"Docker login timed out after {timeout}s")
        except TempDockerAuthError:
            raise
        except Exception as e:
            raise TempDockerAuthError(f"Docker login error: {e}")


    def pull(self, image: str, timeout: int = 600) -> bytes:
        """Pulling the image using a temporary configuration directory

        Args:
            image: Image name
            timeout: Timeout (seconds)

        Returns:
            Command output

        Raises:
            TempDockerAuthError: Pull failed
        """
        if not self._temp_dir:
            raise TempDockerAuthError("Temp dir not created. Call create() first.")
        try:
            result = subprocess.run(
                [
                    "docker",
                    "--config", str(self._temp_dir),
                    "pull",
                    image
                ],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace')
                raise TempDockerAuthError(f"Docker pull failed: {stderr}")

            logger.info(f"Successfully pulled image {image} using temp config")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise TempDockerAuthError(f"Docker pull timed out after {timeout}s")
        except TempDockerAuthError:
            raise
        except Exception as e:
            raise TempDockerAuthError(f"Docker pull error: {e}")

    def is_image_available(self, image: str) -> bool:
        """Check if the image is available (using temporary configuration).

        Args:
            image: Image name

        Returns:
            True if image is available locally, False otherwise
        """
        if not self._temp_dir:
            return False
        try:
            subprocess.check_call(
                [
                    "docker",
                    "--config", str(self._temp_dir),
                    "inspect",
                    image
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            return True
        except subprocess.CalledProcessError:
            return False

@contextmanager
def temp_docker_auth_context(
    registry: str | None = None,
    username: str | None = None,
    password: str | None = None,
    base_dir: str | None = None
) -> Generator[TempDockerAuth, None, None]:
    """Temporary Docker Authentication Context Manager

    Example Usage:
        with temp_docker_auth_context("registry.com", "user", "pass") as auth:
            auth.pull("registry.com/app:v1")
        # Automatically clean up the temporary directory

    Args:
        registry: Registry address (optional)
        username: Username (optional)
        password: Password (optional)
        base_dir: Parent directory of the temporary directory (optional)

    Yields:
        TempDockerAuth Instance
    """
    auth = TempDockerAuth(base_dir=base_dir)
    try:
        auth.create()
        if registry and username and password:
            auth.login(registry, username, password)
        yield auth
    finally:
        auth.cleanup()