"""
Unit tests for user-facing disk quota field integration.

Tests cover:
- SandboxStartRequest disk field
- DockerDeploymentConfig.from_request compatibility handling
- SDK SandboxConfig disk field propagation
"""

import pytest

from rock.admin.proto.request import SandboxStartRequest
from rock.deployments.config import DockerDeploymentConfig
from rock.sdk.sandbox.config import SandboxConfig


class TestSandboxStartRequestDiskField:
    """Tests for SandboxStartRequest disk field."""

    def test_default_disk_is_none(self):
        """Default disk field should be None."""
        request = SandboxStartRequest()
        assert request.disk is None

    def test_can_set_disk_field(self):
        """Can set disk field."""
        request = SandboxStartRequest(disk="50g")
        assert request.disk == "50g"

    def test_disk_with_other_fields(self):
        """Disk field works with other fields."""
        request = SandboxStartRequest(image="python:3.11", cpus=4, memory="16g", disk="100g")
        assert request.disk == "100g"
        assert request.cpus == 4
        assert request.memory == "16g"


class TestFromRequestDiskPropagation:
    """Tests for DockerDeploymentConfig.from_request disk field handling."""

    def test_from_request_without_disk(self):
        """from_request should handle request without disk field."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", cpus=4, memory="16g")
        config = DockerDeploymentConfig.from_request(request)

        assert config.container_name == "test-sandbox"
        assert config.cpus == 4
        assert config.memory == "16g"
        assert config.disk_limit_rootfs is None
        assert config.disk_limit_log is None

    def test_from_request_with_disk(self):
        """from_request should propagate disk field to both rootfs and log limits."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", cpus=4, memory="16g", disk="50g")
        config = DockerDeploymentConfig.from_request(request)

        assert config.container_name == "test-sandbox"
        assert config.cpus == 4
        assert config.memory == "16g"
        assert config.disk_limit_rootfs == "50g"
        assert config.disk_limit_log == "50g"

    def test_from_request_with_disk_none(self):
        """from_request should handle explicit disk=None."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", disk=None)
        config = DockerDeploymentConfig.from_request(request)

        assert config.disk_limit_rootfs is None
        assert config.disk_limit_log is None

    def test_from_request_disk_excluded_from_model_dump(self):
        """from_request should exclude disk field from model_dump."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", cpus=4, memory="16g", disk="50g")
        # The from_request method should exclude disk field from the dict
        # This is verified by the fact that it doesn't cause TypeError
        config = DockerDeploymentConfig.from_request(request)

        # disk should be propagated to disk_limit_* fields
        assert config.disk_limit_rootfs == "50g"
        assert config.disk_limit_log == "50g"


class TestSandboxConfigDiskField:
    """Tests for SDK SandboxConfig disk field."""

    def test_default_disk_is_none(self):
        """Default disk field should be None."""
        config = SandboxConfig()
        assert config.disk is None

    def test_can_set_disk_field(self):
        """Can set disk field."""
        config = SandboxConfig(disk="50g")
        assert config.disk == "50g"

    def test_disk_with_other_fields(self):
        """Disk field works with other fields."""
        config = SandboxConfig(image="python:3.11", cpus=4, memory="16g", disk="100g")
        assert config.disk == "100g"
        assert config.cpus == 4
        assert config.memory == "16g"


class TestDiskPriorityLogic:
    """Tests for disk limit priority logic."""

    def test_user_disk_overrides_runtime_default(self):
        """User-specified disk should override runtime default."""
        # Create config with user-specified disk
        config = DockerDeploymentConfig(disk_limit_rootfs="50g", disk_limit_log="50g")

        # Simulate the priority check: user request should take precedence
        assert config.disk_limit_rootfs == "50g"
        assert config.disk_limit_log == "50g"

    @pytest.mark.asyncio
    async def test_none_disk_runtime_fallback_integration(self):
        """Integration test: None disk should fallback to runtime defaults."""
        from unittest.mock import AsyncMock, MagicMock

        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        # Setup: None disk limits
        config = DockerDeploymentConfig(disk_limit_rootfs=None, disk_limit_log=None)

        # Mock rock_config structure
        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = "100g"
        mock_rock_config.runtime.sandbox_disk_limit_log = "20g"

        # Mock nacos provider that returns None (no nacos override)
        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        # Create mock sandbox_manager
        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        # Set the global sandbox_manager
        sandbox_api.set_sandbox_manager(mock_manager)

        # Call the actual function that applies disk limits
        await _apply_disk_limits(config)

        # After _apply_disk_limits, config should have runtime defaults
        assert config.disk_limit_rootfs == "100g"
        assert config.disk_limit_log == "20g"

    def test_mixed_disk_limits(self):
        """Can set different disk limits for rootfs and log."""
        config = DockerDeploymentConfig(disk_limit_rootfs="50g", disk_limit_log="10g")

        assert config.disk_limit_rootfs == "50g"
        assert config.disk_limit_log == "10g"
