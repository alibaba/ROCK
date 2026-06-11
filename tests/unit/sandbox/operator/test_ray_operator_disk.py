"""Unit tests for Ray operator disk resource scheduling.

Tests verify that _generate_actor_options correctly propagates
disk as a Ray custom resource named "disk" (in bytes).
"""

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


class TestRayOperatorDiskResource:
    """Tests for RayOperator._generate_actor_options disk handling."""

    def _make_operator(self):
        from unittest.mock import MagicMock

        from rock.config import RuntimeConfig

        return RayOperator(ray_service=MagicMock(), runtime_config=RuntimeConfig())

    def test_no_disk_resource_when_disk_limit_is_none(self):
        operator = self._make_operator()
        config = DockerDeploymentConfig(container_name="test-1", cpus=2, memory="4g")
        opts = operator._generate_actor_options(config)

        assert opts["num_cpus"] == 2
        assert "resources" not in opts

    def test_disk_resource_set_when_disk_limit_specified(self):
        operator = self._make_operator()
        config = DockerDeploymentConfig(container_name="test-2", cpus=2, memory="4g", disk="50g")
        opts = operator._generate_actor_options(config)

        assert opts["resources"]["disk"] == 50 * 1024**3

    def test_disk_resource_with_pin_to_host(self):
        operator = self._make_operator()
        config = DockerDeploymentConfig(container_name="test-3", cpus=2, memory="4g", disk="20g")
        opts = operator._generate_actor_options(config, pin_to_host_ip="10.0.0.1")

        assert opts["resources"]["disk"] == 20 * 1024**3
        assert opts["resources"]["node:10.0.0.1"] == 0.001

    def test_pin_to_host_without_disk(self):
        operator = self._make_operator()
        config = DockerDeploymentConfig(container_name="test-4", cpus=2, memory="4g")
        opts = operator._generate_actor_options(config, pin_to_host_ip="10.0.0.1")

        assert opts["resources"] == {"node:10.0.0.1": 0.001}
        assert "disk" not in opts["resources"]

    def test_invalid_disk_raises_error(self):
        from rock.sdk.common.exceptions import BadRequestRockError

        operator = self._make_operator()
        config = DockerDeploymentConfig(container_name="test-5", cpus=2, memory="4g", disk="invalid")

        with pytest.raises(BadRequestRockError):
            operator._generate_actor_options(config)
