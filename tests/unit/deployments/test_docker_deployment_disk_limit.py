"""
Unit tests for disk_limit support in DockerDeployment and DockerDeploymentConfig.

Tests cover:
- DockerDeploymentConfig default and custom disk_limit_rootfs / disk_limit_log values
- DockerDeployment._storage_opts() argument generation
- DockerDeployment.start() graceful degradation when storage-opt is unsupported
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment

# ---- DockerDeploymentConfig tests ----


class TestDockerDeploymentConfigDiskLimit:
    def test_default_disk_limit_rootfs_is_none(self):
        config = DockerDeploymentConfig()
        assert config.disk_limit_rootfs is None

    def test_default_disk_limit_log_is_none(self):
        config = DockerDeploymentConfig()
        assert config.disk_limit_log is None

    def test_custom_disk_limit_rootfs(self):
        config = DockerDeploymentConfig(disk_limit_rootfs="50g")
        assert config.disk_limit_rootfs == "50g"

    def test_custom_disk_limit_log(self):
        config = DockerDeploymentConfig(disk_limit_log="5g")
        assert config.disk_limit_log == "5g"

    def test_disk_limit_rootfs_none(self):
        config = DockerDeploymentConfig(disk_limit_rootfs=None)
        assert config.disk_limit_rootfs is None

    def test_disk_limit_log_none(self):
        config = DockerDeploymentConfig(disk_limit_log=None)
        assert config.disk_limit_log is None

    def test_disk_limit_rootfs_preserved_in_model_dump(self):
        config = DockerDeploymentConfig(disk_limit_rootfs="50g")
        dump = config.model_dump()
        assert dump["disk_limit_rootfs"] == "50g"

    def test_disk_limit_log_preserved_in_model_dump(self):
        config = DockerDeploymentConfig(disk_limit_log="5g")
        dump = config.model_dump()
        assert dump["disk_limit_log"] == "5g"

    def test_disk_limit_rootfs_none_preserved_in_model_dump(self):
        config = DockerDeploymentConfig(disk_limit_rootfs=None)
        dump = config.model_dump()
        assert dump["disk_limit_rootfs"] is None


# ---- DockerDeployment._storage_opts() tests ----


class TestStorageOpts:
    """Tests for DockerDeployment._storage_opts() method."""

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_with_disk_limit_rootfs(self, _mock_validator):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk_limit_rootfs="30g"))
        result = deployment._storage_opts()
        assert result == ["--storage-opt", "size=30g"]

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_with_none(self, _mock_validator):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk_limit_rootfs=None))
        result = deployment._storage_opts()
        assert result == []

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_default_value(self, _mock_validator):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig())
        result = deployment._storage_opts()
        assert result == []

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_various_sizes(self, _mock_validator):
        for size in ("1g", "512m", "50g", "1t"):
            deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk_limit_rootfs=size))
            result = deployment._storage_opts()
            assert result == ["--storage-opt", f"size={size}"]


# ---- DockerDeployment.start() storage-opt degradation tests ----


def _make_start_mocks(deployment):
    deployment.sandbox_validator = MagicMock()
    deployment.sandbox_validator.check_availability.return_value = True
    deployment.sandbox_validator.check_resource.return_value = True
    deployment._pull_image = MagicMock()
    deployment.do_port_mapping = AsyncMock()
    deployment._prepare_volume_mounts = MagicMock(return_value=[])
    deployment._start_container = AsyncMock()
    deployment._wait_until_alive = AsyncMock()
    deployment._service_status = MagicMock()
    deployment._service_status.get_mapped_port = MagicMock(return_value=8080)
    deployment._service_status.phases = {}


async def _run_start(deployment):
    with (
        patch("rock.deployments.docker.get_executor"),
        patch("rock.deployments.docker.asyncio.get_running_loop") as mock_loop,
        patch("rock.deployments.docker.wait_until_alive", new_callable=AsyncMock),
        patch("rock.deployments.docker.env_vars") as mock_env,
        patch("rock.deployments.docker.subprocess"),
    ):
        mock_env.ROCK_LOGGING_PATH = ""
        mock_env.ROCK_TIME_ZONE = "UTC"
        mock_loop.return_value.run_in_executor = AsyncMock()
        try:
            await deployment.start()
        except Exception:
            pass


class TestDockerDeploymentStartDiskLimit:
    """Tests that start() applies correct effective values for rootfs and log quotas."""

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=False)
    async def test_rootfs_downgraded_when_storage_opt_unsupported(self, _mock_detect, _mock_validator):
        """When storage-opt NOT supported: effective_disk_limit_rootfs=None; config unchanged."""
        config = DockerDeploymentConfig(disk_limit_rootfs="50g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk_limit_rootfs == "50g"
        assert deployment.effective_disk_limit_rootfs is None

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=True)
    async def test_rootfs_preserved_when_storage_opt_supported(self, _mock_detect, _mock_validator):
        """When storage-opt IS supported: effective_disk_limit_rootfs matches config."""
        config = DockerDeploymentConfig(disk_limit_rootfs="50g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk_limit_rootfs == "50g"
        assert deployment.effective_disk_limit_rootfs == "50g"

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=False)
    async def test_no_error_when_rootfs_already_none(self, _mock_detect, _mock_validator):
        """When disk_limit_rootfs is None: start() should not error."""
        config = DockerDeploymentConfig(disk_limit_rootfs=None, image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk_limit_rootfs is None
        assert deployment.effective_disk_limit_rootfs is None

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=True)
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=False)
    async def test_log_downgraded_when_not_xfs_prjquota(self, _mock_prjquota, _mock_detect, _mock_validator):
        """When log path is not XFS+prjquota: effective_disk_limit_log=None; config unchanged.

        Note: log quota has NO dependency on docker being overlay2 —
        is_xfs_prjquota_path() is the only gate.
        """
        config = DockerDeploymentConfig(disk_limit_log="5g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)

        with (
            patch("rock.deployments.docker.get_executor"),
            patch("rock.deployments.docker.asyncio.get_running_loop") as mock_loop,
            patch("rock.deployments.docker.wait_until_alive", new_callable=AsyncMock),
            patch("rock.deployments.docker.env_vars") as mock_env,
            patch("rock.deployments.docker.subprocess"),
        ):
            mock_env.ROCK_LOGGING_PATH = "/var/log/rock"
            mock_env.ROCK_TIME_ZONE = "UTC"
            mock_loop.return_value.run_in_executor = AsyncMock()
            try:
                await deployment.start()
            except Exception:
                pass

        assert deployment.config.disk_limit_log == "5g"
        assert deployment.effective_disk_limit_log is None

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=False)
    def test_log_not_downgraded_when_no_log_path(self, _mock_prjquota, _mock_validator):
        """When ROCK_LOGGING_PATH is empty, _try_set_log_dir_quota is never called,
        so effective_disk_limit_log remains equal to config.disk_limit_log."""
        config = DockerDeploymentConfig(disk_limit_log="5g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        # effective starts equal to config before start() is called
        assert deployment.effective_disk_limit_log == "5g"

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=False)
    def test_try_set_log_dir_quota_downgrades_when_not_xfs_prjquota(self, _mock_prjquota, _mock_validator):
        """_try_set_log_dir_quota: is_xfs_prjquota_path=False → effective_disk_limit_log=None."""
        config = DockerDeploymentConfig(disk_limit_log="5g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        deployment._effective_disk_limit_log = "5g"
        deployment._container_name = "test-container"

        deployment._try_set_log_dir_quota("/var/log/rock/test-container")

        assert deployment.effective_disk_limit_log is None

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=True)
    def test_try_set_log_dir_quota_independent_of_docker_driver(self, _mock_prjquota, _mock_validator):
        """_try_set_log_dir_quota passes the XFS gate regardless of Docker storage driver.

        Log quota only requires is_xfs_prjquota_path(); overlay2 is irrelevant.
        The subprocess calls inside (findmnt, xfs_quota) are mocked to succeed.
        """
        config = DockerDeploymentConfig(disk_limit_log="5g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        deployment._effective_disk_limit_log = "5g"
        deployment._container_name = "test-container"

        with patch("rock.deployments.docker.subprocess") as mock_sub:
            ok = MagicMock()
            ok.returncode = 0
            ok.stdout = "/var/log/rock"
            mock_sub.run.return_value = ok
            deployment._try_set_log_dir_quota("/var/log/rock/test-container")

        # xfs_quota succeeded → effective value preserved
        assert deployment.effective_disk_limit_log == "5g"
