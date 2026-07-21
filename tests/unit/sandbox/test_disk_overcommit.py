"""
Unit tests for disk overcommit support.

Tests cover:
- _apply_disk_limits overcommit ratio propagation
- Ray scheduling disk resource division by overcommit ratio
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.utils.format import parse_size_to_bytes

# ---- _apply_disk_limits overcommit tests ----


class TestApplyDiskLimitsOvercommit:
    @pytest.mark.asyncio
    async def test_overcommit_ratio_from_nacos(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk="20g")

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = None

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(
            side_effect=lambda key: {"sandbox_disk_overcommit_ratio": "2.0"}.get(key)
        )
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk == "20g"
        assert config.disk_overcommit_ratio == 2.0

    @pytest.mark.asyncio
    async def test_overcommit_ratio_from_runtime_config(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk="20g")

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = 1.5

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk == "20g"
        assert config.disk_overcommit_ratio == 1.5

    @pytest.mark.asyncio
    async def test_nacos_ratio_overrides_runtime(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk="10g")

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = 1.5

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(
            side_effect=lambda key: {"sandbox_disk_overcommit_ratio": "3.0"}.get(key)
        )
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk == "10g"
        assert config.disk_overcommit_ratio == 3.0

    @pytest.mark.asyncio
    async def test_no_overcommit_when_ratio_none(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk="20g")

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = None

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk == "20g"
        assert config.disk_overcommit_ratio is None

    @pytest.mark.asyncio
    async def test_no_overcommit_when_ratio_le_one(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk="20g")

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = 1.0

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk == "20g"
        assert config.disk_overcommit_ratio is None

    @pytest.mark.asyncio
    async def test_no_overcommit_when_disk_none(self):
        from rock.admin.entrypoints import sandbox_api
        from rock.admin.entrypoints.sandbox_api import _apply_disk_limits

        config = DockerDeploymentConfig(disk=None)

        mock_rock_config = MagicMock()
        mock_rock_config.runtime.sandbox_disk_limit_rootfs = None
        mock_rock_config.runtime.sandbox_disk_overcommit_ratio = 2.0

        mock_nacos = AsyncMock()
        mock_nacos.get_config_value = AsyncMock(return_value=None)
        mock_rock_config.nacos_provider = mock_nacos

        mock_manager = MagicMock()
        mock_manager.rock_config = mock_rock_config

        sandbox_api.set_sandbox_manager(mock_manager)
        await _apply_disk_limits(config)

        assert config.disk is None
        assert config.disk_overcommit_ratio is None


# ---- Ray scheduling overcommit tests ----


class TestRaySchedulingOvercommit:
    """Test that Ray actor options divide disk by overcommit ratio."""

    def test_ray_operator_divides_disk_by_ratio(self):
        from rock.sandbox.operator.ray import RayOperator

        config = DockerDeploymentConfig(disk="40g", disk_overcommit_ratio=2.0, container_name="test-sb")
        operator = RayOperator.__new__(RayOperator)
        operator._disk_scheduling_enabled = True
        options = operator._generate_actor_options(config)

        expected_bytes = int(parse_size_to_bytes("40g") / 2.0)
        assert options["resources"]["disk"] == expected_bytes

    def test_ray_operator_no_ratio(self):
        from rock.sandbox.operator.ray import RayOperator

        config = DockerDeploymentConfig(disk="40g", container_name="test-sb")
        operator = RayOperator.__new__(RayOperator)
        operator._disk_scheduling_enabled = True
        options = operator._generate_actor_options(config)

        assert options["resources"]["disk"] == parse_size_to_bytes("40g")

    def test_ray_operator_ratio_le_one_ignored(self):
        from rock.sandbox.operator.ray import RayOperator

        config = DockerDeploymentConfig(disk="40g", disk_overcommit_ratio=0.5, container_name="test-sb")
        operator = RayOperator.__new__(RayOperator)
        operator._disk_scheduling_enabled = True
        options = operator._generate_actor_options(config)

        assert options["resources"]["disk"] == parse_size_to_bytes("40g")

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_docker_uses_full_disk_regardless_of_ratio(self, _mock_validator):
        """Docker storage-opt should use the full disk value, not divided."""
        from rock.deployments.docker import DockerDeployment

        config = DockerDeploymentConfig(disk="40g", disk_overcommit_ratio=2.0)
        deployment = DockerDeployment.from_config(config)

        assert deployment._storage_opts() == ["--storage-opt", "size=40g"]
