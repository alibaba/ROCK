"""
Unit tests for rock/utils/docker_auth.py

Tests cover:
- TempDockerAuth class: create, login, pull, cleanup, is_image_available
- temp_docker_auth_context context manager
- TempDockerAuthError exception
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rock.utils.docker_auth import TempDockerAuth, TempDockerAuthError, temp_docker_auth_context


class TestTempDockerAuthInit:
    """Tests for TempDockerAuth initialization."""

    def test_init_default(self):
        """Test default initialization without base_dir."""
        auth = TempDockerAuth()
        assert auth._base_dir is None
        assert auth._temp_dir is None

    def test_init_with_base_dir(self):
        """Test initialization with custom base_dir."""
        auth = TempDockerAuth(base_dir="/tmp/custom")
        assert auth._base_dir == "/tmp/custom"
        assert auth._temp_dir is None


class TestTempDockerAuthProperties:
    """Tests for TempDockerAuth properties."""

    def test_temp_dir_before_create(self):
        """Test temp_dir returns None before create()."""
        auth = TempDockerAuth()
        assert auth.temp_dir is None

    def test_temp_dir_after_create(self):
        """Test temp_dir returns Path after create()."""
        auth = TempDockerAuth()
        auth.create()
        assert auth.temp_dir is not None
        assert isinstance(auth.temp_dir, Path)
        auth.cleanup()

    def test_config_path_before_create(self):
        """Test config_path returns None before create()."""
        auth = TempDockerAuth()
        assert auth.config_path is None

    def test_config_path_after_create(self):
        """Test config_path returns correct Path after create()."""
        auth = TempDockerAuth()
        auth.create()
        assert auth.config_path is not None
        assert auth.config_path.name == "config.json"
        assert auth.config_path.parent == auth.temp_dir
        auth.cleanup()


class TestTempDockerAuthCreate:
    """Tests for TempDockerAuth.create()."""

    def test_create_default_location(self):
        """Test create() with default location."""
        auth = TempDockerAuth()
        path = auth.create()
        
        assert path is not None
        assert path.exists()
        assert path.is_dir()
        assert "rock_docker_auth_" in path.name
        
        auth.cleanup()

    def test_create_custom_location(self):
        """Test create() with custom base_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            auth = TempDockerAuth(base_dir=tmpdir)
            path = auth.create()
            
            assert path is not None
            assert path.exists()
            assert str(path).startswith(tmpdir)
            
            auth.cleanup()

    def test_create_custom_location_creates_parent(self):
        """Test create() creates parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "nested" / "dir"
            auth = TempDockerAuth(base_dir=str(custom_path))
            path = auth.create()
            
            assert path is not None
            assert path.exists()
            assert custom_path.exists()
            
            auth.cleanup()


class TestTempDockerAuthCleanup:
    """Tests for TempDockerAuth.cleanup()."""

    def test_cleanup_removes_directory(self):
        """Test cleanup() removes the temporary directory."""
        auth = TempDockerAuth()
        auth.create()
        path = auth.temp_dir
        
        assert path.exists()
        
        auth.cleanup()
        
        assert not path.exists()
        assert auth._temp_dir is None

    def test_cleanup_safe_when_no_temp_dir(self):
        """Test cleanup() is safe when no temp_dir exists."""
        auth = TempDockerAuth()
        # Should not raise
        auth.cleanup()

    def test_cleanup_safe_when_directory_deleted(self):
        """Test cleanup() is safe when directory was already deleted."""
        auth = TempDockerAuth()
        auth.create()
        path = auth.temp_dir
        
        # Delete the directory manually
        import shutil
        shutil.rmtree(path)
        
        # Should not raise
        auth.cleanup()
        assert auth._temp_dir is None


class TestTempDockerAuthLogin:
    """Tests for TempDockerAuth.login()."""

    def test_login_without_create_raises(self):
        """Test login() raises error if create() not called."""
        auth = TempDockerAuth()
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.login("registry.example.com", "user", "pass")
        assert "Temp dir not created" in str(exc_info.value)

    @patch("subprocess.run")
    def test_login_success(self, mock_run):
        """Test successful login."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        auth = TempDockerAuth()
        auth.create()
        auth.login("registry.example.com", "user", "password")
        
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "docker" in call_args[0][0]
        assert "--config" in call_args[0][0]
        assert "login" in call_args[0][0]
        assert "registry.example.com" in call_args[0][0]
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_login_failure(self, mock_run):
        """Test login failure with non-zero return code."""
        mock_run.return_value = MagicMock(
            returncode=1, 
            stderr="Error: authentication failed"
        )

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.login("registry.example.com", "user", "wrongpass")
        assert "Docker login failed" in str(exc_info.value)
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_login_timeout(self, mock_run):
        """Test login timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.login("registry.example.com", "user", "pass")
        assert "timed out" in str(exc_info.value)
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_login_unexpected_error(self, mock_run):
        """Test login with unexpected error."""
        mock_run.side_effect = OSError("Unexpected error")

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.login("registry.example.com", "user", "pass")
        assert "Docker login error" in str(exc_info.value)
        
        auth.cleanup()


class TestTempDockerAuthPull:
    """Tests for TempDockerAuth.pull()."""

    def test_pull_without_create_raises(self):
        """Test pull() raises error if create() not called."""
        auth = TempDockerAuth()
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.pull("registry.example.com/image:v1")
        assert "Temp dir not created" in str(exc_info.value)

    @patch("subprocess.run")
    def test_pull_success(self, mock_run):
        """Test successful pull."""
        mock_run.return_value = MagicMock(
            returncode=0, 
            stdout=b"pulled successfully",
            stderr=b""
        )

        auth = TempDockerAuth()
        auth.create()
        result = auth.pull("python:3.11")
        
        assert result == b"pulled successfully"
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "docker" in call_args[0][0]
        assert "--config" in call_args[0][0]
        assert "pull" in call_args[0][0]
        assert "python:3.11" in call_args[0][0]
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_pull_failure(self, mock_run):
        """Test pull failure with non-zero return code."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"Error: image not found"
        )

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.pull("nonexistent/image:v1")
        assert "Docker pull failed" in str(exc_info.value)
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_pull_timeout(self, mock_run):
        """Test pull timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=600)

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.pull("large-image:v1")
        assert "timed out" in str(exc_info.value)
        
        auth.cleanup()

    @patch("subprocess.run")
    def test_pull_unexpected_error(self, mock_run):
        """Test pull with unexpected error."""
        mock_run.side_effect = OSError("Unexpected error")

        auth = TempDockerAuth()
        auth.create()
        
        with pytest.raises(TempDockerAuthError) as exc_info:
            auth.pull("image:v1")
        assert "Docker pull error" in str(exc_info.value)
        
        auth.cleanup()


class TestTempDockerAuthIsImageAvailable:
    """Tests for TempDockerAuth.is_image_available()."""

    def test_is_image_available_without_create(self):
        """Test is_image_available returns False if create() not called."""
        auth = TempDockerAuth()
        assert auth.is_image_available("python:3.11") is False

    @patch("subprocess.check_call")
    def test_is_image_available_true(self, mock_check_call):
        """Test is_image_available returns True for existing image."""
        mock_check_call.return_value = 0

        auth = TempDockerAuth()
        auth.create()
        result = auth.is_image_available("python:3.11")
        
        assert result is True
        mock_check_call.assert_called_once()
        call_args = mock_check_call.call_args
        assert "docker" in call_args[0][0]
        assert "inspect" in call_args[0][0]
        assert "python:3.11" in call_args[0][0]
        
        auth.cleanup()

    @patch("subprocess.check_call")
    def test_is_image_available_false(self, mock_check_call):
        """Test is_image_available returns False for non-existing image."""
        mock_check_call.side_effect = subprocess.CalledProcessError(1, "docker")

        auth = TempDockerAuth()
        auth.create()
        result = auth.is_image_available("nonexistent:v1")
        
        assert result is False
        
        auth.cleanup()


class TestTempDockerAuthContext:
    """Tests for temp_docker_auth_context context manager."""

    def test_context_creates_and_cleans_up(self):
        """Test context manager creates and cleans up temp dir."""
        with temp_docker_auth_context() as auth:
            assert auth.temp_dir is not None
            assert auth.temp_dir.exists()
            path = auth.temp_dir
        
        assert not path.exists()

    def test_context_with_credentials(self):
        """Test context manager with credentials calls login."""
        with patch.object(TempDockerAuth, 'login') as mock_login:
            with temp_docker_auth_context(
                registry="registry.example.com",
                username="user",
                password="pass"
            ) as auth:
                assert auth.temp_dir is not None
            
            mock_login.assert_called_once_with("registry.example.com", "user", "pass")

    def test_context_cleanup_on_exception(self):
        """Test context manager cleans up on exception."""
        with pytest.raises(ValueError):
            with temp_docker_auth_context() as auth:
                path = auth.temp_dir
                raise ValueError("Test error")
        
        assert not path.exists()

    def test_context_with_base_dir(self):
        """Test context manager with custom base_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with temp_docker_auth_context(base_dir=tmpdir) as auth:
                assert str(auth.temp_dir).startswith(tmpdir)
                path = auth.temp_dir
            
            assert not path.exists()


class TestTempDockerAuthError:
    """Tests for TempDockerAuthError exception."""

    def test_error_is_exception(self):
        """Test TempDockerAuthError is an Exception."""
        assert issubclass(TempDockerAuthError, Exception)

    def test_error_message(self):
        """Test TempDockerAuthError preserves message."""
        error = TempDockerAuthError("Test error message")
        assert str(error) == "Test error message"

    def test_error_can_be_raised_and_caught(self):
        """Test TempDockerAuthError can be raised and caught."""
        with pytest.raises(TempDockerAuthError):
            raise TempDockerAuthError("Test error")


class TestTempDockerAuthIntegration:
    """Integration tests that verify method interactions."""

    def test_full_lifecycle(self):
        """Test full lifecycle: create -> login -> pull -> cleanup."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=b"success",
                stderr=b""
            )
            
            auth = TempDockerAuth()
            auth.create()
            
            # Login
            auth.login("registry.example.com", "user", "pass")
            assert mock_run.call_count == 1
            
            # Pull
            auth.pull("registry.example.com/image:v1")
            assert mock_run.call_count == 2
            
            # Cleanup
            auth.cleanup()
            assert auth.temp_dir is None

    def test_multiple_create_cleanup_cycles(self):
        """Test multiple create/cleanup cycles work correctly."""
        auth = TempDockerAuth()
        
        for i in range(3):
            auth.create()
            assert auth.temp_dir is not None
            path = auth.temp_dir
            assert path.exists()
            
            auth.cleanup()
            assert not path.exists()
            assert auth.temp_dir is None
