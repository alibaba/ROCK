"""Unit tests for K8sOperator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from rock.sandbox.operator.k8s.operator import K8sOperator
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import K8sConfig
from rock.deployments.config import DeploymentConfig
from rock.deployments.abstract import AbstractDeployment


pytestmark = pytest.mark.skipif(not K8S_AVAILABLE, reason="kubernetes library not available")


class MockDeploymentConfig(DeploymentConfig):
    """Mock deployment config for testing."""
    image: str = "python:3.11"
    cpus: float = 2
    memory: str = "4Gi"
    container_name: str | None = None
    template_name: str = "default"
    auto_clear_time_minutes: int = 30
    
    def get_deployment(self) -> AbstractDeployment:
        """Mock implementation."""
        return MagicMock()


@pytest.fixture
def k8s_config():
    """Create K8sConfig with required templates."""
    return K8sConfig(
        kubeconfig_path=None,
        templates={
            "default": {
                "namespace": "rock-test",
                "ports": {
                    "proxy": 8000,
                    "server": 8080,
                    "ssh": 22,
                },
                "template": {
                    "metadata": {"labels": {"app": "test"}},
                    "spec": {"containers": [{"name": "main", "image": "python:3.11"}]},
                },
            }
        },
    )


@pytest.fixture
def mock_provider():
    """Create mock BatchSandboxProvider."""
    provider = AsyncMock()
    return provider


@pytest.fixture
def k8s_operator(k8s_config, mock_provider):
    """Create K8sOperator instance with mock provider."""
    with patch("rock.sandbox.operator.k8s.operator.BatchSandboxProvider", return_value=mock_provider):
        operator = K8sOperator(k8s_config=k8s_config)
        operator._provider = mock_provider
        return operator


@pytest.fixture
def deployment_config():
    """Create deployment configuration."""
    return MockDeploymentConfig(
        image="python:3.11",
        cpus=2,
        memory="4Gi",
        container_name="test-sandbox",
        template_name="default",
    )


class TestK8sOperator:
    """Test cases for K8sOperator."""

    def test_initialization(self, k8s_config):
        """Test K8sOperator initialization."""
        with patch("rock.sandbox.operator.k8s.operator.BatchSandboxProvider"):
            operator = K8sOperator(k8s_config=k8s_config)
            assert operator._provider is not None

    def test_initialization_without_templates(self):
        """Test K8sOperator initialization fails without templates."""
        config = K8sConfig(kubeconfig_path=None, templates={})
        # Validation happens in provider now, so operator init succeeds
        # but provider creation should fail
        with pytest.raises(ValueError, match="No templates provided"):
            from rock.sandbox.operator.k8s.provider import BatchSandboxProvider
            BatchSandboxProvider(k8s_config=config)

    @pytest.mark.asyncio
    async def test_submit_success(self, k8s_operator, mock_provider, deployment_config):
        """Test successful sandbox submission."""
        # Mock provider's submit method
        mock_sandbox_info = {
            "sandbox_id": "test-sandbox",
            "host_ip": "10.0.0.1",
            "state": State.RUNNING,
            "user_id": "test-user",
            "image": "python:3.11",
            "cpus": 2,
            "memory": "4Gi",
            "port_mapping": {22555: 8000, 8080: 8080, 22: 22},
        }
        mock_provider.submit = AsyncMock(return_value=SandboxInfo(**mock_sandbox_info))

        result = await k8s_operator.submit(deployment_config, user_info={"user_id": "test-user"})

        assert result["sandbox_id"] == "test-sandbox"
        assert result["host_ip"] == "10.0.0.1"
        assert result["state"] == State.RUNNING
        assert result["user_id"] == "test-user"

        mock_provider.submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_submit_no_host_ip(self, k8s_operator, mock_provider, deployment_config):
        """Test submission fails when no host IP is allocated."""
        # Mock provider to raise exception
        mock_provider.submit = AsyncMock(
            side_effect=Exception("Failed to get host IP for sandbox test-sandbox")
        )

        with pytest.raises(Exception, match="Failed to get host IP"):
            await k8s_operator.submit(deployment_config)

    @pytest.mark.asyncio
    async def test_submit_with_cleanup_on_failure(self, k8s_operator, mock_provider, deployment_config):
        """Test that failed submission is handled by provider."""
        mock_provider.submit = AsyncMock(side_effect=Exception("K8S API error"))

        with pytest.raises(Exception, match="K8S API error"):
            await k8s_operator.submit(deployment_config)

    @pytest.mark.asyncio
    async def test_get_status_success(self, k8s_operator, mock_provider):
        """Test successful status retrieval."""
        mock_sandbox_info = {
            "sandbox_id": "test-sandbox",
            "host_name": "test-sandbox",
            "host_ip": "10.0.0.1",
            "state": State.RUNNING,
            "image": "python:3.11",
            "alive": True,
            "port_mapping": {},
        }
        mock_provider.get_status = AsyncMock(return_value=SandboxInfo(**mock_sandbox_info))

        result = await k8s_operator.get_status("test-sandbox")

        assert result["sandbox_id"] == "test-sandbox"
        assert result["state"] == State.RUNNING
        assert result["alive"] is True

    @pytest.mark.asyncio
    async def test_get_status_not_alive(self, k8s_operator, mock_provider):
        """Test status when sandbox is not alive."""
        mock_sandbox_info = {
            "sandbox_id": "test-sandbox",
            "host_name": "test-sandbox",
            "host_ip": "10.0.0.1",
            "state": State.PENDING,
            "alive": False,
            "port_mapping": {},
        }
        mock_provider.get_status = AsyncMock(return_value=SandboxInfo(**mock_sandbox_info))

        result = await k8s_operator.get_status("test-sandbox")

        assert result["sandbox_id"] == "test-sandbox"
        assert result["state"] == State.PENDING
        assert result["alive"] is False

    @pytest.mark.asyncio
    async def test_get_status_not_found(self, k8s_operator, mock_provider):
        """Test status retrieval when sandbox not found."""
        mock_provider.get_status = AsyncMock(
            side_effect=Exception("Sandbox test-sandbox not found")
        )

        with pytest.raises(Exception, match="not found"):
            await k8s_operator.get_status("test-sandbox")

    @pytest.mark.asyncio
    async def test_get_status_missing_ports_annotation(self, k8s_operator, mock_provider):
        """Test that missing ports annotation raises error."""
        # Mock get_status to raise ValueError for missing ports
        mock_provider.get_status = AsyncMock(
            side_effect=ValueError("Sandbox 'test-sandbox' is missing required 'rock.sandbox/ports' annotation")
        )

        with pytest.raises(Exception, match="missing required.*annotation"):
            await k8s_operator.get_status("test-sandbox")

    @pytest.mark.asyncio
    async def test_stop_success(self, k8s_operator, mock_provider):
        """Test successful sandbox stop."""
        mock_provider.stop = AsyncMock(return_value=True)

        result = await k8s_operator.stop("test-sandbox")

        assert result is True
        mock_provider.stop.assert_awaited_once_with("test-sandbox")

    @pytest.mark.asyncio
    async def test_stop_failure(self, k8s_operator, mock_provider):
        """Test sandbox stop failure."""
        mock_provider.stop = AsyncMock(return_value=False)

        result = await k8s_operator.stop("test-sandbox")
        
        assert result is False


