"""Unit tests for DockerDeployment._docker_update_resources()."""

from unittest.mock import patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment


@pytest.fixture
def _patch_validator():
    with patch("rock.deployments.docker.DockerSandboxValidator"):
        yield


@pytest.mark.usefixtures("_patch_validator")
class TestDockerUpdateResources:
    """Tests for _docker_update_resources command construction and error handling."""

    def _make_deployment(self, **kwargs) -> DockerDeployment:
        config = DockerDeploymentConfig(container_name="test-sandbox", **kwargs)
        return DockerDeployment.from_config(config)

    def test_without_limit_cpus(self):
        deployment = self._make_deployment(cpus=2, memory="4g")
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            deployment._docker_update_resources()

        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "docker", "update",
            "--cpus=2.0",
            "--memory=4g",
            "--memory-swap=4g",
            "test-sandbox",
        ]

    def test_with_limit_cpus(self):
        deployment = self._make_deployment(cpus=2, limit_cpus=4, memory="4g")
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            deployment._docker_update_resources()

        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "docker", "update",
            "--cpu-shares=2048",
            "--cpus=4.0",
            "--memory=4g",
            "--memory-swap=4g",
            "test-sandbox",
        ]

    def test_fractional_cpus_shares(self):
        deployment = self._make_deployment(cpus=0.5, limit_cpus=1.5, memory="2g")
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            deployment._docker_update_resources()

        cmd = mock_run.call_args[0][0]
        assert "--cpu-shares=512" in cmd
        assert "--cpus=1.5" in cmd

    def test_failure_raises_runtime_error(self):
        deployment = self._make_deployment(cpus=2, memory="4g")
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "no such container"
            with pytest.raises(RuntimeError, match="docker update failed"):
                deployment._docker_update_resources()

    def test_consistent_with_cpus_method(self):
        """_docker_update_resources cpu flags should match _cpus() output."""
        for cpus, limit_cpus in [(2, None), (2, 4), (0.5, 1.5)]:
            deployment = self._make_deployment(cpus=cpus, limit_cpus=limit_cpus, memory="4g")
            with patch("rock.deployments.docker.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                deployment._docker_update_resources()

            update_cmd = mock_run.call_args[0][0]
            cpu_flags_from_update = [f for f in update_cmd if "cpu" in f.lower()]
            cpu_flags_from_cpus = deployment._cpus()
            assert cpu_flags_from_update == cpu_flags_from_cpus
