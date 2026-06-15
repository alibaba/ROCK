"""
Unit tests for SandboxManager.validate_sandbox_spec() — disk validation.

These tests do NOT require Ray or Docker; they only test the synchronous
validation logic.
"""

import pytest

from rock.config import RuntimeConfig, StandardSpec
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def runtime_config():
    return RuntimeConfig(
        max_allowed_spec=StandardSpec(cpus=16, memory="64g"),
    )


class TestValidateSandboxSpecDiskLimit:
    """Tests for disk validation in SandboxManager.validate_sandbox_spec()."""

    def test_valid_disk_20g(self, runtime_config):
        config = DockerDeploymentConfig(disk="20g")
        # Should not raise
        SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_valid_disk_various_formats(self, runtime_config):
        for size in ("1g", "512m", "100gb", "2t", "1024mb", "1024k"):
            config = DockerDeploymentConfig(disk=size)
            SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_valid_disk_none(self, runtime_config):
        """None disk should skip validation (no error)."""
        config = DockerDeploymentConfig(disk=None)
        SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_invalid_disk_raises_bad_request(self, runtime_config):
        config = DockerDeploymentConfig(disk="not-a-size")
        with pytest.raises(BadRequestRockError, match="Invalid disk size"):
            SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_invalid_disk_empty_string(self, runtime_config):
        config = DockerDeploymentConfig(disk="")
        with pytest.raises(BadRequestRockError, match="Invalid disk size"):
            SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_invalid_disk_negative(self, runtime_config):
        config = DockerDeploymentConfig(disk="-10g")
        with pytest.raises(BadRequestRockError, match="Invalid disk size"):
            SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_invalid_disk_no_unit(self, runtime_config):
        """A bare number without unit should still be parsed (as bytes)."""
        config = DockerDeploymentConfig(disk="1024")
        # Bare number is treated as bytes by parse_size_to_bytes, so it should pass
        SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_invalid_disk_only_unit(self, runtime_config):
        config = DockerDeploymentConfig(disk="gb")
        with pytest.raises(BadRequestRockError, match="Invalid disk size"):
            SandboxManager.validate_sandbox_spec(None, runtime_config, config)

    def test_disk_validation_independent_of_cpu_memory(self, runtime_config):
        """disk validation should not interfere with cpu/memory checks."""
        config = DockerDeploymentConfig(cpus=2, memory="8g", disk="50g")
        SandboxManager.validate_sandbox_spec(None, runtime_config, config)
