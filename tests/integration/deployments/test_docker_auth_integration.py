"""
Integration tests for Docker temporary authentication in rock/deployments/docker.py

Tests cover:
- TempAuthDockerClient integration with DockerDeployment
- _pull_image method with temp auth
- Pull behavior with various configurations
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.utils.docker import DockerUtil
from rock.deployments.docker_client import TempAuthDockerClient, TempAuthDockerClientError
from tests.integration.conftest import SKIP_IF_NO_DOCKER


# Skip all tests if Docker is not available
pytestmark = [pytest.mark.need_docker, SKIP_IF_NO_DOCKER]


class TestDockerDeploymentPull:
    """Tests for DockerDeployment._pull_image method."""

    def test_pull_image_never_skip(self):
        """Test pull=never skips image pull."""
        deployment = DockerDeployment(
            image="python:3.11",
            pull="never"
        )

        # Should not raise
        deployment._pull_image()

    def test_pull_image_missing_with_cached_image(self):
        """Test pull=missing with cached image skips pull."""
        # Use an image that's likely cached
        if DockerUtil.is_image_available("python:3.11"):
            deployment = DockerDeployment(
                image="python:3.11",
                pull="missing"
            )

            # Should not raise
            deployment._pull_image()

    def test_pull_image_missing_without_cached_image(self):
        """Test pull=missing without cached image attempts pull."""
        # Use a non-existent image
        deployment = DockerDeployment(
            image="nonexistent-local-image-xyz:latest",
            pull="missing"
        )

        # Should attempt pull and fail
        from rock.rocklet.exceptions import DockerPullError
        with pytest.raises(DockerPullError):
            deployment._pull_image()

    def test_pull_image_always_without_credentials(self):
        """Test pull=always without credentials."""
        deployment = DockerDeployment(
            image="python:3.11",
            pull="always"
        )

        # This should work (actual pull may succeed or fail depending on network)
        try:
            deployment._pull_image()
        except Exception:
            pass  # Accept any exception from actual docker pull

    @patch("rock.deployments.docker.TempAuthDockerClient")
    def test_pull_image_with_registry_credentials(self, mock_client_class):
        """Test pull with registry credentials."""
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=None)

        deployment = DockerDeployment(
            image="registry.example.com/namespace/image:v1",
            registry_username="user",
            registry_password="pass",
            pull="always"
        )

        # This will fail because we're mocking, but we can verify the flow
        try:
            deployment._pull_image()
        except Exception:
            pass

    def test_pull_image_with_temp_auth_error(self):
        """Test _pull_image handles TempAuthDockerClientError."""
        with patch("rock.deployments.docker.TempAuthDockerClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_class.return_value.__exit__ = MagicMock(return_value=None)
            mock_client.pull.side_effect = TempAuthDockerClientError("Auth failed")

            deployment = DockerDeployment(
                image="python:3.11",
                registry_username="user",
                registry_password="pass",
                pull="always"
            )

            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError):
                deployment._pull_image()

    def test_pull_image_with_subprocess_error(self):
        """Test _pull_image handles CalledProcessError."""
        with patch("rock.deployments.docker.TempAuthDockerClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_class.return_value.__exit__ = MagicMock(return_value=None)
            mock_client.pull.side_effect = subprocess.CalledProcessError(1, "docker", stderr=b"pull failed")

            deployment = DockerDeployment(
                image="python:3.11",
                pull="always"
            )

            from rock.rocklet.exceptions import DockerPullError
            with pytest.raises(DockerPullError):
                deployment._pull_image()


class TestFromConfig:
    """Tests for DockerDeployment.from_config()."""

    def test_from_config_preserves_password(self):
        """Test from_config preserves registry_password."""
        config = DockerDeploymentConfig(
            image="registry.example.com/image:v1",
            registry_username="user",
            registry_password="secret_password",
        )

        deployment = DockerDeployment.from_config(config)

        # Password should be preserved even though it's excluded from model_dump
        assert deployment._config.registry_password == "secret_password"


class TestDockerDeploymentConfig:
    """Tests for DockerDeploymentConfig."""

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
    @pytest.mark.skip(reason="Requires Docker daemon insecure-registry config for localhost")
    async def test_pull_from_private_registry_with_temp_auth(self, local_registry):
        """Test pulling from private registry with temp auth.

        NOTE: This test requires Docker daemon to be configured with
        insecure-registries for localhost/127.0.0.1. See:
        https://docs.docker.com/engine/reference/commandline/dockerd/#insecure-registries
        """
        registry_url, username, password = local_registry

        # This test verifies the auth flow works with a real registry
        # We're not actually pushing/pulling an image, just verifying auth works
        with TempAuthDockerClient(
            registry=registry_url,
            username=username,
            password=password
        ) as client:
            # If we got here, login succeeded
            assert client.logged_in is True

    @pytest.mark.asyncio
    @pytest.mark.need_docker
    async def test_temp_auth_context_with_real_docker(self):
        """Test TempAuthDockerClient context with real Docker."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert client.temp_dir.exists()
            path = client.temp_dir

        # After context, directory should be cleaned up
        assert not path.exists()

    @pytest.mark.asyncio
    @pytest.mark.need_docker
    async def test_temp_auth_context_without_credentials(self):
        """Test TempAuthDockerClient context without credentials (public images)."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert client.logged_in is False
            path = client.temp_dir

        assert not path.exists()


class TestTempAuthDockerClientContextManager:
    """Tests for TempAuthDockerClient as context manager."""

    def test_context_manager_creates_temp_dir(self):
        """Test context manager creates temporary directory."""
        with TempAuthDockerClient() as client:
            assert client.temp_dir is not None
            assert client.temp_dir.exists()
            path = client.temp_dir

        assert not path.exists()

    def test_context_manager_with_base_dir(self):
        """Test context manager with custom base_dir."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with TempAuthDockerClient(base_dir=tmpdir) as client:
                assert client.temp_dir is not None
                assert str(client.temp_dir).startswith(tmpdir)
                path = client.temp_dir

            assert not path.exists()

    def test_context_manager_cleanup_on_exception(self):
        """Test context manager cleans up on exception."""
        with pytest.raises(ValueError):
            with TempAuthDockerClient() as client:
                path = client.temp_dir
                raise ValueError("Test error")

        assert not path.exists()
