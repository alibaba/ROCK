"""
Unit tests for rock/deployments/docker_client.py

Tests cover:
- TempAuthDockerClient class: context manager, login, pull, is_image_available
- TempAuthDockerClientError exception
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rock.deployments.docker_client import (
    TempAuthDockerClient,
    TempAuthDockerClientError,
)


class TestTempAuthDockerClientInit:
    """Tests for TempAuthDockerClient initialization."""

    def test_init_default(self):
        """Test default initialization."""
        client = TempAuthDockerClient()
        assert client._registry is None
        assert client._username is None
        assert client._password is None
        assert client._temp_dir is None
        assert client._logged_in is False

    def test_init_with_credentials(self):
        """Test initialization with credentials."""
        client = TempAuthDockerClient(
            registry="registry.example.com",
            username="user",
            password="pass"
        )
        assert client._registry == "registry.example.com"
        assert client._username == "user"
        assert client._password == "pass"

    def test_init_with_base_dir(self):
        """Test initialization with custom base_dir."""
        client = TempAuthDockerClient(base_dir="/tmp/custom")
        assert client._base_dir == "/tmp/custom"


class TestTempAuthDockerClientProperties:
    """Tests for TempAuthDockerClient properties."""

    def test_temp_dir_before_enter(self):
        """Test temp_dir returns None before __enter__."""
        client = TempAuthDockerClient()
        assert client.temp_dir is None

    def test_temp_dir_after_enter(self):
        """Test temp_dir returns Path after __enter__."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert isinstance(client.temp_dir, Path)

    def test_config_path_before_enter(self):
        """Test config_path returns None before __enter__."""
        client = TempAuthDockerClient()
        assert client.config_path is None

    def test_config_path_after_enter(self):
        """Test config_path returns correct Path after __enter__."""
        with TempAuthDockerClient() as client:
            assert client.config_path is not None
            assert client.config_path.name == "config.json"
            assert client.config_path.parent == client.temp_dir

    def test_logged_in_property(self):
        """Test logged_in property."""
        client = TempAuthDockerClient()
        assert client.logged_in is False


class TestTempAuthDockerClientContextManager:
    """Tests for TempAuthDockerClient as context manager."""

    def test_context_creates_and_cleans_up(self):
        """Test context manager creates and cleans up temp dir."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert client.temp_dir.exists()
            path = client.temp_dir

        assert not path.exists()

    def test_context_creates_temp_dir_default_location(self):
        """Test context manager creates temp dir in default location."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert client.temp_dir.exists()
            assert "rock_docker_auth_" in client.temp_dir.name

    def test_context_creates_temp_dir_custom_location(self):
        """Test context manager creates temp dir in custom location."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with TempAuthDockerClient(base_dir=tmpdir) as client:
                assert client.temp_dir is not None
                assert client.temp_dir.exists()
                assert str(client.temp_dir).startswith(tmpdir)

    def test_context_creates_parent_directories(self):
        """Test context manager creates parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "nested" / "dir"
            with TempAuthDockerClient(base_dir=str(custom_path)) as client:
                assert client.temp_dir is not None
                assert client.temp_dir.exists()
                assert custom_path.exists()

    def test_context_cleanup_on_exception(self):
        """Test context manager cleans up on exception."""
        with pytest.raises(ValueError):
            with TempAuthDockerClient() as client:
                path = client.temp_dir
                raise ValueError("Test error")

        assert not path.exists()

    def test_context_cleanup_safe_when_directory_deleted(self):
        """Test cleanup is safe when directory was already deleted."""
        with TempAuthDockerClient() as client:
            path = client.temp_dir
            # Directory exists
            assert path.exists()
        # After context, directory should be cleaned up
        assert not path.exists()


class TestTempAuthDockerClientLogin:
    """Tests for TempAuthDockerClient login functionality."""

    def test_login_with_credentials_on_enter(self):
        """Test login is called automatically when credentials provided."""
        with patch.object(TempAuthDockerClient, '_login') as mock_login:
            with TempAuthDockerClient(
                registry="registry.example.com",
                username="user",
                password="pass"
            ) as client:
                pass

            mock_login.assert_called_once()

    def test_login_without_credentials(self):
        """Test no login when credentials not provided."""
        with TempAuthDockerClient() as client:
            assert client._logged_in is False

    @patch("subprocess.run")
    def test_login_success(self, mock_run):
        """Test successful login."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with TempAuthDockerClient(
            registry="registry.example.com",
            username="user",
            password="password"
        ) as client:
            assert client._logged_in is True
            mock_run.assert_called()
            call_args = mock_run.call_args
            assert "docker" in call_args[0][0]
            assert "login" in call_args[0][0]
            assert "registry.example.com" in call_args[0][0]

    @patch("subprocess.run")
    def test_login_failure(self, mock_run):
        """Test login failure with non-zero return code."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Error: authentication failed"
        )

        with pytest.raises(TempAuthDockerClientError) as exc_info:
            with TempAuthDockerClient(
                registry="registry.example.com",
                username="user",
                password="wrongpass"
            ):
                pass

        assert "Docker login failed" in str(exc_info.value)

    @patch("subprocess.run")
    def test_login_timeout(self, mock_run):
        """Test login timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)

        with pytest.raises(TempAuthDockerClientError) as exc_info:
            with TempAuthDockerClient(
                registry="registry.example.com",
                username="user",
                password="pass"
            ):
                pass

        assert "timed out" in str(exc_info.value)

    @patch("subprocess.run")
    def test_login_unexpected_error(self, mock_run):
        """Test login with unexpected error."""
        mock_run.side_effect = OSError("Unexpected error")

        with pytest.raises(TempAuthDockerClientError) as exc_info:
            with TempAuthDockerClient(
                registry="registry.example.com",
                username="user",
                password="pass"
            ):
                pass

        assert "Docker login error" in str(exc_info.value)


class TestTempAuthDockerClientPull:
    """Tests for TempAuthDockerClient.pull()."""

    def test_pull_without_context_raises(self):
        """Test pull() raises error if not in context."""
        client = TempAuthDockerClient()
        with pytest.raises(TempAuthDockerClientError) as exc_info:
            client.pull("registry.example.com/image:v1")
        assert "Temp dir not created" in str(exc_info.value)

    @patch("subprocess.run")
    def test_pull_success(self, mock_run):
        """Test successful pull."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b"pulled successfully",
            stderr=b""
        )

        with TempAuthDockerClient() as client:
            result = client.pull("python:3.11")

        assert result == b"pulled successfully"
        mock_run.assert_called()
        call_args = mock_run.call_args
        assert "docker" in call_args[0][0]
        assert "pull" in call_args[0][0]
        assert "python:3.11" in call_args[0][0]

    @patch("subprocess.run")
    def test_pull_failure(self, mock_run):
        """Test pull failure with non-zero return code."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"Error: image not found"
        )

        with TempAuthDockerClient() as client:
            with pytest.raises(TempAuthDockerClientError) as exc_info:
                client.pull("nonexistent/image:v1")

        assert "Docker pull failed" in str(exc_info.value)

    @patch("subprocess.run")
    def test_pull_timeout(self, mock_run):
        """Test pull timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=600)

        with TempAuthDockerClient() as client:
            with pytest.raises(TempAuthDockerClientError) as exc_info:
                client.pull("large-image:v1")

        assert "timed out" in str(exc_info.value)

    @patch("subprocess.run")
    def test_pull_unexpected_error(self, mock_run):
        """Test pull with unexpected error."""
        mock_run.side_effect = OSError("Unexpected error")

        with TempAuthDockerClient() as client:
            with pytest.raises(TempAuthDockerClientError) as exc_info:
                client.pull("image:v1")

        assert "Docker pull error" in str(exc_info.value)


class TestTempAuthDockerClientIsImageAvailable:
    """Tests for TempAuthDockerClient.is_image_available()."""

    def test_is_image_available_without_context(self):
        """Test is_image_available returns False if not in context."""
        client = TempAuthDockerClient()
        assert client.is_image_available("python:3.11") is False

    @patch("subprocess.check_call")
    def test_is_image_available_true(self, mock_check_call):
        """Test is_image_available returns True for existing image."""
        mock_check_call.return_value = 0

        with TempAuthDockerClient() as client:
            result = client.is_image_available("python:3.11")

        assert result is True
        mock_check_call.assert_called()
        call_args = mock_check_call.call_args
        assert "docker" in call_args[0][0]
        assert "inspect" in call_args[0][0]
        assert "python:3.11" in call_args[0][0]

    @patch("subprocess.check_call")
    def test_is_image_available_false(self, mock_check_call):
        """Test is_image_available returns False for non-existing image."""
        mock_check_call.side_effect = subprocess.CalledProcessError(1, "docker")

        with TempAuthDockerClient() as client:
            result = client.is_image_available("nonexistent:v1")

        assert result is False


class TestTempAuthDockerClientError:
    """Tests for TempAuthDockerClientError exception."""

    def test_error_is_exception(self):
        """Test TempAuthDockerClientError is an Exception."""
        assert issubclass(TempAuthDockerClientError, Exception)

    def test_error_message(self):
        """Test TempAuthDockerClientError preserves message."""
        error = TempAuthDockerClientError("Test error message")
        assert str(error) == "Test error message"

    def test_error_can_be_raised_and_caught(self):
        """Test TempAuthDockerClientError can be raised and caught."""
        with pytest.raises(TempAuthDockerClientError):
            raise TempAuthDockerClientError("Test error")


class TestIntegration:
    """Integration tests that verify method interactions."""

    @patch("subprocess.run")
    def test_full_lifecycle_with_credentials(self, mock_run):
        """Test full lifecycle: enter -> login -> pull -> exit."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b"success",
            stderr=b""
        )

        with TempAuthDockerClient(
            registry="registry.example.com",
            username="user",
            password="pass"
        ) as client:
            # Login called on enter
            assert client._logged_in is True

            # Pull
            client.pull("registry.example.com/image:v1")
            assert mock_run.call_count == 2

        # After exit, temp_dir should be None
        assert client._temp_dir is None

    def test_multiple_context_cycles(self):
        """Test multiple context cycles work correctly."""
        client = TempAuthDockerClient()

        for i in range(3):
            with client:
                assert client.temp_dir is not None
                path = client.temp_dir
                assert path.exists()

            assert not path.exists()
            assert client._temp_dir is None
