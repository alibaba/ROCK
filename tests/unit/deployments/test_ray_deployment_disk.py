"""Unit tests for RayDeployment._generate_actor_options disk resource."""

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.ray import RayDeployment


class TestRayDeploymentDiskResource:
    """Tests for RayDeployment._generate_actor_options disk handling."""

    def _make_deployment(self, **kwargs):
        from unittest.mock import patch

        config = DockerDeploymentConfig(**kwargs)
        with patch("rock.deployments.docker.DockerSandboxValidator"):
            return RayDeployment.from_config(config)

    def test_no_disk_resource_when_disk_limit_is_none(self):
        deployment = self._make_deployment(container_name="test-1", cpus=2, memory="4g")
        opts = deployment._generate_actor_options("sandbox-test-1")

        assert opts["num_cpus"] == 2
        assert "resources" not in opts

    def test_disk_resource_set_when_disk_limit_specified(self):
        deployment = self._make_deployment(container_name="test-2", cpus=2, memory="4g", disk="50g")
        opts = deployment._generate_actor_options("sandbox-test-2")

        assert opts["resources"]["disk"] == 50 * 1024**3

    def test_invalid_disk_raises_error(self):
        from rock.sdk.common.exceptions import BadRequestRockError

        deployment = self._make_deployment(container_name="test-3", cpus=2, memory="4g", disk="invalid")

        with pytest.raises(BadRequestRockError):
            deployment._generate_actor_options("sandbox-test-3")
