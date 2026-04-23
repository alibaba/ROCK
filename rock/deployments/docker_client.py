"""
Docker client with temporary authentication.

This module provides TempAuthDockerClient - a context manager for isolated Docker
authentication that uses temporary configuration directories to ensure:
1. Credentials do not pollute the user's ~/.docker/config.json
2. Credentials are completely isolated per sandbox
3. Automatic cleanup - credentials are not persistent
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from rock import env_vars

logger = logging.getLogger(__name__)


class TempAuthDockerClientError(Exception):
    """Exception raised for Docker temporary authentication errors."""
    pass


class TempAuthDockerClient:
    """Docker client with temporary authentication configuration.

    A context manager that provides isolated Docker operations using a temporary
    configuration directory. This ensures credentials are isolated per sandbox
    and automatically cleaned up.

    Usage:
        with TempAuthDockerClient(
            registry="registry.example.com",
            username="user",
            password="pass"
        ) as client:
            client.pull("registry.example.com/image:v1")
            if client.is_image_available("registry.example.com/image:v1"):
                print("Image pulled successfully")
        # Temporary directory and credentials are automatically cleaned up

    Or without credentials (for public images):
        with TempAuthDockerClient() as client:
            client.pull("python:3.11")
    """

    def __init__(
        self,
        registry: str | None = None,
        username: str | None = None,
        password: str | None = None,
        base_dir: str | None = None,
    ):
        """Initialize the temporary Docker client.

        Args:
            registry: Docker registry URL (e.g., registry.example.com).
                     Required if username/password are provided.
            username: Registry username for authentication.
            password: Registry password for authentication.
            base_dir: Parent directory for the temporary config directory.
                     Defaults to ROCK_DOCKER_TEMP_AUTH_DIR env var or system temp.
        """
        self._registry = registry
        self._username = username
        self._password = password
        self._base_dir = base_dir or env_vars.ROCK_DOCKER_TEMP_AUTH_DIR
        self._temp_dir: Path | None = None
        self._logged_in = False

    @property
    def temp_dir(self) -> Path | None:
        """Get the temporary directory path."""
        return self._temp_dir

    @property
    def config_path(self) -> Path | None:
        """Get the temporary config.json path."""
        if self._temp_dir:
            return self._temp_dir / "config.json"
        return None

    def __enter__(self) -> "TempAuthDockerClient":
        """Create temporary directory and optionally login to registry."""
        self._create_temp_dir()
        
        # Perform login if credentials are provided
        if self._registry and self._username and self._password:
            self._login()
        
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Clean up temporary directory."""
        self._cleanup()
        return None  # Don't suppress exceptions

    def _create_temp_dir(self) -> None:
        """Create the temporary configuration directory."""
        prefix = "rock_docker_auth_"
        if self._base_dir:
            Path(self._base_dir).mkdir(parents=True, exist_ok=True)
            self._temp_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=self._base_dir))
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix=prefix))
        
        logger.debug(f"Created temp docker config dir: {self._temp_dir}")

    def _cleanup(self) -> None:
        """Clean up the temporary configuration directory."""
        if self._temp_dir:
            if self._temp_dir.exists():
                try:
                    shutil.rmtree(self._temp_dir, ignore_errors=True)
                    logger.debug(f"Cleaned up temp docker config dir: {self._temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp docker config dir: {e}")
            # Clear reference even if deletion failed
            self._temp_dir = None
            self._logged_in = False

    def _login(self) -> None:
        """Login to the Docker registry."""
        if not self._temp_dir:
            raise TempAuthDockerClientError("Temp dir not created. Use as context manager.")
        
        if not self._registry or not self._username or not self._password:
            raise TempAuthDockerClientError("Registry, username, and password required for login.")
        
        try:
            result = subprocess.run(
                [
                    "docker",
                    "--config", str(self._temp_dir),
                    "login",
                    self._registry,
                    "-u", self._username,
                    "--password-stdin"
                ],
                input=self._password,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                error_msg = f"Docker login failed: {result.stderr.strip()}"
                logger.error(error_msg)
                raise TempAuthDockerClientError(error_msg)

            self._logged_in = True
            logger.info(f"Successfully logged in to {self._registry} using temp config")
        except subprocess.TimeoutExpired:
            raise TempAuthDockerClientError("Docker login timed out after 30s")
        except TempAuthDockerClientError:
            raise
        except Exception as e:
            raise TempAuthDockerClientError(f"Docker login error: {e}")

    def pull(self, image: str, timeout: int = 600) -> bytes:
        """Pull a Docker image.

        Args:
            image: Image name to pull (e.g., "python:3.11" or "registry.example.com/app:v1")
            timeout: Timeout in seconds (default: 600s = 10 minutes)

        Returns:
            Command stdout as bytes

        Raises:
            TempAuthDockerClientError: If pull fails
        """
        if not self._temp_dir:
            raise TempAuthDockerClientError("Temp dir not created. Use as context manager.")
        
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
                raise TempAuthDockerClientError(f"Docker pull failed: {stderr}")

            logger.info(f"Successfully pulled image {image} using temp config")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise TempAuthDockerClientError(f"Docker pull timed out after {timeout}s")
        except TempAuthDockerClientError:
            raise
        except Exception as e:
            raise TempAuthDockerClientError(f"Docker pull error: {e}")

    def is_image_available(self, image: str) -> bool:
        """Check if an image is available locally.

        Args:
            image: Image name to check

        Returns:
            True if image is available, False otherwise
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

    @property
    def logged_in(self) -> bool:
        """Check if client has logged in to a registry."""
        return self._logged_in
