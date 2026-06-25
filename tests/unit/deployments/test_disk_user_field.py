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
        request = SandboxStartRequest(image="python:3.11")
        assert request.disk is None

    def test_can_set_disk_field(self):
        """Can set disk field."""
        request = SandboxStartRequest(image="python:3.11", disk="50g")
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
        assert config.disk is None

    def test_from_request_with_disk(self):
        """from_request should propagate disk field to config.disk."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", cpus=4, memory="16g", disk="50g")
        config = DockerDeploymentConfig.from_request(request)

        assert config.container_name == "test-sandbox"
        assert config.cpus == 4
        assert config.memory == "16g"
        assert config.disk == "50g"

    def test_from_request_with_disk_none(self):
        """from_request should handle explicit disk=None."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", disk=None)
        config = DockerDeploymentConfig.from_request(request)

        assert config.disk is None

    def test_from_request_disk_propagated(self):
        """from_request should propagate disk field directly."""
        request = SandboxStartRequest(sandbox_id="test-sandbox", image="python:3.11", cpus=4, memory="16g", disk="50g")
        config = DockerDeploymentConfig.from_request(request)

        assert config.disk == "50g"


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
        config = DockerDeploymentConfig(disk="50g")
        assert config.disk == "50g"

    @pytest.mark.asyncio
    async def test_none_disk_runtime_fallback_integration(self):
        """Integration test: None disk should fallback to runtime defaults."""
        from unittest.mock import AsyncMock, MagicMock

        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk=None)

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = "100g"
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = None

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)

        await _apply_disk_limits(config)

        assert config.disk == "100g"
        assert config.disk_overcommit_ratio is None
