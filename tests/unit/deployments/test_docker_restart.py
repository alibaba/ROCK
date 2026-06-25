"""Tests for DockerDeployment.restart() phase handling."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.constants import Status
from rock.deployments.docker import DockerDeployment


@pytest.fixture
def deployment():
    with patch("rock.deployments.docker.env_vars") as mock_env:
        mock_env.ROCK_WORKER_ENV_TYPE = "docker"
        mock_env.ROCK_SERVICE_STATUS_DIR = "/tmp/test_status"
        d = DockerDeployment(container_name="sb-1", image="python:3.11", port=22555)
        return d


class TestRestartPhases:
    @pytest.mark.asyncio
    async def test_image_pull_is_success_after_restart(self, deployment):
        """Restart reuses existing container — image_pull should be marked SUCCESS."""
        status_data = {
            "phases": {
                "image_pull": {"status": "success", "message": "image pull success"},
                "docker_run": {"status": "success", "message": "docker run success"},
            },
            "port_mapping": {"22555": 22555},
        }

        mock_popen = MagicMock()
        with (
            patch.object(deployment, "_docker_start", return_value=mock_popen),
            patch("os.path.exists", return_value=True),
            patch("builtins.open", create=True) as mock_open,
            patch.object(deployment, "_wait_until_alive", new_callable=AsyncMock),
            patch("rock.deployments.docker.RemoteSandboxRuntime") as mock_runtime_cls,
        ):
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            mock_open.return_value.read = MagicMock(return_value=json.dumps(status_data))
            mock_runtime = MagicMock()
            mock_runtime_cls.from_config.return_value = mock_runtime

            # Patch json.load to return status_data
            with patch("json.load", return_value=status_data):
                await deployment.restart()

        phase = deployment._service_status.get_phase("image_pull")
        assert phase.status == Status.SUCCESS, f"Expected image_pull to be SUCCESS after restart, got {phase.status}"
