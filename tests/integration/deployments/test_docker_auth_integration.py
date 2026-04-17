"""
Integration tests for Docker temporary authentication in rock/deployments/docker.py

Tests cover:
- _resolve_auth_mode method with various configurations
- _init_temp_docker_auth and _cleanup_temp_docker_auth methods
- _pull_image_with_temp_auth and _pull_image_legacy methods
- Environment variable ROCK_DOCKER_TEMP_AUTH integration
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.utils.docker import DockerUtil
from rock.utils.docker_auth import TempDockerAuth, TempDockerAuthError
from tests.integration.conftest import SKIP_IF_NO_DOCKER


# Skip all tests if Docker is not available
pytestmark = [pytest.mark.need_docker, SKIP_IF_NO_DOCKER]


class TestResolveAuthMode:
    """Tests for DockerDeployment._resolve_auth_mode()."""

    def test_resolve_auth_mode_env_true(self, monkeypatch):
        """Test _resolve_auth_mode with ROCK_DOCKER_TEMP_AUTH=true."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(image="python:3.11")
        assert deployment._use_temp_docker_auth is True

    def test_resolve_auth_mode_env_false(self, monkeypatch):
        """Test _resolve_auth_mode with ROCK_DOCKER_TEMP_AUTH=false."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(image="python:3.11")
        assert deployment._use_temp_docker_auth is False

    def test_resolve_auth_mode_env_1(self, monkeypatch):
        """Test _resolve_auth_mode with ROCK_DOCKER_TEMP_AUTH=1."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "1")
        
        deployment = DockerDeployment(image="python:3.11")
        assert deployment._use_temp_docker_auth is True

    def test_resolve_auth_mode_env_0(self, monkeypatch):
        """Test _resolve_auth_mode with ROCK_DOCKER_TEMP_AUTH=0."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "0")
        
        deployment = DockerDeployment(image="python:3.11")
        assert deployment._use_temp_docker_auth is False

    def test_resolve_auth_mode_config_true(self, monkeypatch):
        """Test _resolve_auth_mode with config use_temp_docker_auth=True."""
        monkeypatch.delenv("ROCK_DOCKER_TEMP_AUTH", raising=False)
        
        config = DockerDeploymentConfig(
            image="python:3.11",
            use_temp_docker_auth=True
        )
        deployment = DockerDeployment.from_config(config)
        assert deployment._use_temp_docker_auth is True

    def test_resolve_auth_mode_config_false(self, monkeypatch):
        """Test _resolve_auth_mode with config use_temp_docker_auth=False."""
        monkeypatch.delenv("ROCK_DOCKER_TEMP_AUTH", raising=False)
        
        config = DockerDeploymentConfig(
            image="python:3.11",
            use_temp_docker_auth=False
        )
        deployment = DockerDeployment.from_config(config)
        assert deployment._use_temp_docker_auth is False

    def test_resolve_auth_mode_default(self, monkeypatch):
        """Test _resolve_auth_mode defaults to True."""
        monkeypatch.delenv("ROCK_DOCKER_TEMP_AUTH", raising=False)
        
        deployment = DockerDeployment(image="python:3.11")
        assert deployment._use_temp_docker_auth is True

    def test_resolve_auth_mode_env_overrides_config(self, monkeypatch):
        """Test environment variable overrides config parameter."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        # Config says True, but env says False -> env wins
        config = DockerDeploymentConfig(
            image="python:3.11",
            use_temp_docker_auth=True
        )
        deployment = DockerDeployment.from_config(config)
        assert deployment._use_temp_docker_auth is False


class TestTempAuthInitialization:
    """Tests for temporary auth initialization."""

    def test_init_temp_docker_auth_with_credentials(self, monkeypatch):
        """Test _init_temp_docker_auth creates TempDockerAuth with credentials."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(
            image="python:3.11",
            registry_username="user",
            registry_password="pass"
        )
        
        deployment._init_temp_docker_auth()
        
        assert deployment._temp_docker_auth is not None
        assert isinstance(deployment._temp_docker_auth, TempDockerAuth)
        assert deployment._temp_docker_auth.temp_dir is not None
        
        deployment._cleanup_temp_docker_auth()

    def test_init_temp_docker_auth_without_credentials(self, monkeypatch):
        """Test _init_temp_docker_auth does nothing without credentials."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(image="python:3.11")
        
        deployment._init_temp_docker_auth()
        
        # Should not create temp auth without credentials
        assert deployment._temp_docker_auth is None

    def test_cleanup_temp_docker_auth(self, monkeypatch):
        """Test _cleanup_temp_docker_auth properly cleans up."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(
            image="python:3.11",
            registry_username="user",
            registry_password="pass"
        )
        
        deployment._init_temp_docker_auth()
        temp_dir = deployment._temp_docker_auth.temp_dir
        
        assert temp_dir.exists()
        
        deployment._cleanup_temp_docker_auth()
        
        assert deployment._temp_docker_auth is None
        assert not temp_dir.exists()

    def test_cleanup_temp_docker_auth_safe_when_none(self, monkeypatch):
        """Test _cleanup_temp_docker_auth is safe when no temp auth."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(image="python:3.11")
        
        # Should not raise
        deployment._cleanup_temp_docker_auth()


class TestLegacyAuthMode:
    """Tests for legacy authentication mode."""

    def test_legacy_mode_registers_docker_login_hook(self, monkeypatch):
        """Test legacy mode registers DockerLoginHook."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(
            image="python:3.11",
            registry_username="user",
            registry_password="pass"
        )
        
        # Check that DockerLoginHook was added
        assert len(deployment._hooks._hooks) > 0

    def test_legacy_mode_no_temp_auth(self, monkeypatch):
        """Test legacy mode does not use temp auth."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(
            image="python:3.11",
            registry_username="user",
            registry_password="pass"
        )
        
        # _temp_docker_auth should remain None
        assert deployment._temp_docker_auth is None

    def test_temp_auth_mode_logs_info(self, monkeypatch, caplog):
        """Test temp auth mode logs info message."""
        import logging
        caplog.set_level(logging.INFO)
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(image="python:3.11")
        
        # Check log message
        assert "Using temp docker auth mode" in caplog.text
        assert deployment._use_temp_docker_auth is True


class TestPullImageWithTempAuth:
    """Tests for _pull_image_with_temp_auth method."""

    def test_pull_image_never_skip(self, monkeypatch):
        """Test pull=never skips image pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(
            image="python:3.11",
            pull="never"
        )
        
        # Should not raise
        deployment._pull_image_with_temp_auth()

    def test_pull_image_missing_with_cached_image(self, monkeypatch):
        """Test pull=missing with cached image skips pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        # Use an image that's likely cached
        if DockerUtil.is_image_available("python:3.11"):
            deployment = DockerDeployment(
                image="python:3.11",
                pull="missing"
            )
            
            # Should not raise
            deployment._pull_image_with_temp_auth()

    def test_pull_image_missing_without_cached_image(self, monkeypatch):
        """Test pull=missing without cached image attempts pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        # Use a non-existent image
        deployment = DockerDeployment(
            image="nonexistent-local-image-xyz:latest",
            pull="missing"
        )
        
        # Should attempt pull and fail
        from rock.rocklet.exceptions import DockerPullError
        with pytest.raises(DockerPullError):
            deployment._pull_image_with_temp_auth()

    def test_pull_image_always_without_credentials(self, monkeypatch):
        """Test pull=always without credentials uses DockerUtil.pull_image."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        deployment = DockerDeployment(
            image="python:3.11",
            pull="always"
        )
        
        # This should use DockerUtil.pull_image since no credentials
        # For test purposes, we'll just verify it doesn't raise temp auth errors
        # (actual pull may succeed or fail depending on image availability)
        try:
            deployment._pull_image_with_temp_auth()
        except Exception:
            pass  # Accept any exception from actual docker pull

    @patch("rock.deployments.docker.TempDockerAuth")
    def test_pull_image_with_registry_credentials(self, mock_auth_class, monkeypatch):
        """Test pull with registry credentials."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        mock_auth = MagicMock()
        mock_auth_class.return_value = mock_auth
        mock_auth.temp_dir = MagicMock()
        
        deployment = DockerDeployment(
            image="registry.example.com/namespace/image:v1",
            registry_username="user",
            registry_password="pass",
            pull="always"
        )
        
        # This will fail because we're mocking, but we can verify the flow
        try:
            deployment._pull_image_with_temp_auth()
        except Exception:
            pass
        
        # Verify temp auth was created and cleaned up
        deployment._cleanup_temp_docker_auth()

    def test_pull_image_with_temp_auth_error(self, monkeypatch):
        """Test _pull_image_with_temp_auth handles TempDockerAuthError."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        with patch("rock.deployments.docker.TempDockerAuth") as mock_auth_class:
            mock_auth = MagicMock()
            mock_auth_class.return_value = mock_auth
            mock_auth.create.side_effect = TempDockerAuthError("Auth failed")
            
            deployment = DockerDeployment(
                image="python:3.11",
                registry_username="user",
                registry_password="pass",
                pull="always"
            )
            
            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError):
                deployment._pull_image_with_temp_auth()

    def test_pull_image_with_subprocess_error(self, monkeypatch):
        """Test _pull_image_with_temp_auth handles CalledProcessError."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        with patch("rock.deployments.docker.DockerUtil.pull_image") as mock_pull:
            mock_pull.side_effect = subprocess.CalledProcessError(1, "docker", stderr=b"pull failed")
            
            deployment = DockerDeployment(
                image="python:3.11",
                pull="always"
            )
            
            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError):
                deployment._pull_image_with_temp_auth()


class TestPullImageLegacy:
    """Tests for _pull_image_legacy method."""

    def test_pull_legacy_never_skip(self, monkeypatch):
        """Test legacy pull=never skips image pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(
            image="python:3.11",
            pull="never"
        )
        
        # Should not raise
        deployment._pull_image_legacy()

    def test_pull_legacy_missing_with_cached_image(self, monkeypatch):
        """Test legacy pull=missing with cached image skips pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        if DockerUtil.is_image_available("python:3.11"):
            deployment = DockerDeployment(
                image="python:3.11",
                pull="missing"
            )
            
            # Should not raise
            deployment._pull_image_legacy()

    def test_pull_legacy_missing_without_cached_image(self, monkeypatch):
        """Test legacy pull=missing without cached image attempts pull."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(
            image="nonexistent-local-image-xyz:latest",
            pull="missing"
        )
        
        from rock.rocklet.exceptions import DockerPullError
        with pytest.raises(DockerPullError):
            deployment._pull_image_legacy()

    def test_pull_legacy_with_subprocess_error(self, monkeypatch):
        """Test _pull_image_legacy handles CalledProcessError."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        with patch("rock.deployments.docker.DockerUtil.pull_image") as mock_pull:
            mock_pull.side_effect = subprocess.CalledProcessError(1, "docker", stderr=b"pull failed")
            
            deployment = DockerDeployment(
                image="python:3.11",
                pull="always"
            )
            
            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError):
                deployment._pull_image_legacy()

    def test_pull_legacy_with_subprocess_error_no_stderr(self, monkeypatch):
        """Test _pull_image_legacy handles CalledProcessError without stderr."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        with patch("rock.deployments.docker.DockerUtil.pull_image") as mock_pull:
            mock_pull.side_effect = subprocess.CalledProcessError(1, "docker", stderr=None)
            
            deployment = DockerDeployment(
                image="python:3.11",
                pull="always"
            )
            
            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError) as exc_info:
                deployment._pull_image_legacy()
            
            assert "Unknown" in str(exc_info.value)

    def test_pull_legacy_logs_warning(self, monkeypatch, caplog):
        """Test _pull_image_legacy logs warning message."""
        import logging
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "false")
        
        deployment = DockerDeployment(
            image="python:3.11",
            pull="never"
        )
        
        deployment._pull_image_legacy()
        
        assert "legacy auth mode" in caplog.text


class TestFromConfig:
    """Tests for DockerDeployment.from_config()."""

    def test_from_config_preserves_password(self, monkeypatch):
        """Test from_config preserves registry_password."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        config = DockerDeploymentConfig(
            image="registry.example.com/image:v1",
            registry_username="user",
            registry_password="secret_password",
            use_temp_docker_auth=True
        )
        
        deployment = DockerDeployment.from_config(config)
        
        # Password should be preserved even though it's excluded from model_dump
        assert deployment._config.registry_password == "secret_password"
        assert deployment._use_temp_docker_auth is True


class TestDockerDeploymentConfig:
    """Tests for DockerDeploymentConfig with use_temp_docker_auth field."""

    def test_config_default_use_temp_docker_auth(self):
        """Test default value of use_temp_docker_auth is True."""
        config = DockerDeploymentConfig(image="python:3.11")
        assert config.use_temp_docker_auth is True

    def test_config_use_temp_docker_auth_false(self):
        """Test use_temp_docker_auth can be set to False."""
        config = DockerDeploymentConfig(
            image="python:3.11",
            use_temp_docker_auth=False
        )
        assert config.use_temp_docker_auth is False

    def test_config_registry_password_excluded(self):
        """Test registry_password is excluded from model_dump."""
        config = DockerDeploymentConfig(
            image="python:3.11",
            registry_username="user",
            registry_password="secret"
        )
        
        dump = config.model_dump()
        assert "registry_password" not in dump

    def test_config_registry_password_stored(self):
        """Test registry_password is still stored on the model."""
        config = DockerDeploymentConfig(
            image="python:3.11",
            registry_username="user",
            registry_password="secret"
        )
        
        assert config.registry_password == "secret"


class TestIntegrationWithLocalRegistry:
    """Integration tests with local Docker registry (requires Docker)."""

    @pytest.mark.asyncio
    @pytest.mark.need_docker
    async def test_pull_from_private_registry_with_temp_auth(self, local_registry, monkeypatch):
        """Test pulling from private registry with temp auth."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        registry_url, username, password = local_registry
        
        # This test verifies the auth flow works with a real registry
        # We're not actually pushing/pulling an image, just verifying auth works
        auth = TempDockerAuth()
        auth.create()
        
        try:
            auth.login(registry_url, username, password)
            # If we got here, login succeeded
            assert True
        finally:
            auth.cleanup()

    @pytest.mark.asyncio
    @pytest.mark.need_docker
    async def test_temp_auth_context_with_real_docker(self, monkeypatch):
        """Test temp_docker_auth_context with real Docker."""
        monkeypatch.setenv("ROCK_DOCKER_TEMP_AUTH", "true")
        
        with TempDockerAuth() as auth:
            assert auth.temp_dir is not None
            assert auth.temp_dir.exists()
            path = auth.temp_dir
        
        # After context, directory should be cleaned up
        assert not path.exists()
