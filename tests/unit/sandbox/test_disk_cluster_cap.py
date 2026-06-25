"""
Unit tests for user-facing disk quota field with cluster cap validation.

These tests do NOT require Ray or Docker; they only test the synchronous
validation logic for the new disk field.
"""

import pytest

from rock.config import RuntimeConfig, StandardSpec
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def runtime_config_with_disk_cap():
    """RuntimeConfig with disk cap set."""
    return RuntimeConfig(
        max_allowed_spec=StandardSpec(cpus=16, memory="64g", disk="256g"),
    )


@pytest.fixture
def runtime_config_without_disk_cap():
    """RuntimeConfig without disk cap (disk=None)."""
    return RuntimeConfig(
        max_allowed_spec=StandardSpec(cpus=16, memory="64g", disk=None),
    )


class TestDiskClusterCapValidation:
    """Tests for disk cluster cap validation."""

    def test_disk_under_cap_allowed(self, runtime_config_with_disk_cap):
        """Disk quota under cap should be allowed."""
        config = DockerDeploymentConfig(disk="128g")
        SandboxManager.validate_sandbox_spec(None, runtime_config_with_disk_cap, config)

    def test_disk_at_cap_allowed(self, runtime_config_with_disk_cap):
        """Disk quota equal to cap should be allowed."""
        config = DockerDeploymentConfig(disk="256g")
        SandboxManager.validate_sandbox_spec(None, runtime_config_with_disk_cap, config)

    def test_disk_exceeds_cap_raises_bad_request(self, runtime_config_with_disk_cap):
        """Disk quota exceeding cap should raise BadRequestRockError."""
        config = DockerDeploymentConfig(disk="512g")
        with pytest.raises(BadRequestRockError, match=r"Requested disk 512g exceeds the maximum allowed 256g"):
            SandboxManager.validate_sandbox_spec(None, runtime_config_with_disk_cap, config)

    def test_disk_exceeds_cap_mixed_units(self, runtime_config_with_disk_cap):
        """Disk quota exceeding cap should raise error with mixed units."""
        config = DockerDeploymentConfig(disk="1t")
        with pytest.raises(BadRequestRockError, match=r"Requested disk 1t exceeds the maximum allowed 256g"):
            SandboxManager.validate_sandbox_spec(None, runtime_config_with_disk_cap, config)

    def test_no_disk_cap_allows_any_size(self, runtime_config_without_disk_cap):
        """When no disk cap is set, any disk size should be allowed."""
        config = DockerDeploymentConfig(disk="1024t")
        SandboxManager.validate_sandbox_spec(None, runtime_config_without_disk_cap, config)

    def test_none_disk_limit_skips_cap_check(self, runtime_config_with_disk_cap):
        """None disk should skip cap validation."""
        config = DockerDeploymentConfig(disk=None)
        SandboxManager.validate_sandbox_spec(None, runtime_config_with_disk_cap, config)
