"""
Unit tests for disk_limit support in DockerDeployment and DockerDeploymentConfig.

Tests cover:
- DockerDeploymentConfig default and custom disk values
- DockerDeployment._storage_opts() argument generation
- DockerDeployment.start() graceful degradation when storage-opt is unsupported
- XFS project quota fallback for containerd image store
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import XFS_PRJID_MIN, XFS_PRJID_RANGE, DockerDeployment

# ---- DockerDeploymentConfig tests ----


class TestDockerDeploymentConfigDiskLimit:
    def test_default_disk_is_none(self):
        config = DockerDeploymentConfig()
        assert config.disk is None

    def test_custom_disk(self):
        config = DockerDeploymentConfig(disk="50g")
        assert config.disk == "50g"

    def test_disk_none(self):
        config = DockerDeploymentConfig(disk=None)
        assert config.disk is None

    def test_disk_preserved_in_model_dump(self):
        config = DockerDeploymentConfig(disk="50g")
        dump = config.model_dump()
        assert dump["disk"] == "50g"

    def test_disk_none_preserved_in_model_dump(self):
        config = DockerDeploymentConfig(disk=None)
        dump = config.model_dump()
        assert dump["disk"] is None


# ---- DockerDeployment._storage_opts() tests ----


class TestStorageOpts:
    """Tests for DockerDeployment._storage_opts() method."""

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_with_disk(self, _mock_validator):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk="30g"))
        result = deployment._storage_opts()
        assert result == ["--storage-opt", "size=30g"]

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_storage_opts_with_none(self, _mock_validator):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk=None))
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
            deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk=size))
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
    """Tests that start() applies correct effective values for rootfs quota."""

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=False)
    async def test_rootfs_downgraded_when_storage_opt_unsupported(self, _mock_detect, _mock_validator):
        """When storage-opt NOT supported: effective_disk=None; config unchanged."""
        config = DockerDeploymentConfig(disk="50g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk == "50g"
        assert deployment.effective_disk is None

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=True)
    async def test_rootfs_preserved_when_storage_opt_supported(self, _mock_detect, _mock_validator):
        """When storage-opt IS supported: effective_disk matches config."""
        config = DockerDeploymentConfig(disk="50g", image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk == "50g"
        assert deployment.effective_disk == "50g"

    @pytest.mark.asyncio
    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.DockerUtil.detect_storage_opt_support", return_value=False)
    async def test_no_error_when_rootfs_already_none(self, _mock_detect, _mock_validator):
        """When disk is None: start() should not error."""
        config = DockerDeploymentConfig(disk=None, image="python:3.11")
        deployment = DockerDeployment.from_config(config)
        _make_start_mocks(deployment)
        await _run_start(deployment)

        assert deployment.config.disk is None
        assert deployment.effective_disk is None


# ---- XFS quota fallback tests (containerd image store) ----


def _make_subprocess_result(returncode=0, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestSetupRootfsQuotaXfs:
    """Tests for DockerDeployment._setup_rootfs_quota_xfs()."""

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def _make_deployment(self, _mock_validator, disk_limit="50g"):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk=disk_limit))
        deployment.set_container_name("test-container-abc")
        deployment._effective_disk = None
        return deployment

    @patch("rock.deployments.docker.subprocess.run")
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=True)
    def test_rootfs_xfs_fallback_sets_quota(self, _mock_xfs, mock_run):
        """When UpperDir is on xfs+prjquota, prjid and bhard should be set."""
        deployment = self._make_deployment()

        mock_run.side_effect = [
            # docker inspect UpperDir
            _make_subprocess_result(stdout="/var/lib/docker/overlay2/abc123/diff"),
            # findmnt for mountpoint
            _make_subprocess_result(stdout="/data"),
            # xfs_quota project -s
            _make_subprocess_result(),
            # xfs_quota limit
            _make_subprocess_result(),
        ]

        deployment._setup_rootfs_quota_xfs()

        assert deployment._rootfs_xfs_prjid is not None
        assert XFS_PRJID_MIN <= deployment._rootfs_xfs_prjid < XFS_PRJID_MIN + XFS_PRJID_RANGE
        assert deployment._rootfs_xfs_mountpoint == "/data"
        assert deployment._rootfs_upper_dir == "/var/lib/docker/overlay2/abc123/diff"

    @patch("rock.deployments.docker.subprocess.run")
    @patch("rock.deployments.docker.DockerUtil.is_xfs_prjquota_path", return_value=False)
    def test_rootfs_xfs_fallback_degrades_when_not_xfs(self, _mock_xfs, mock_run):
        """When UpperDir is not on xfs+prjquota, should degrade gracefully."""
        deployment = self._make_deployment()

        mock_run.return_value = _make_subprocess_result(stdout="/var/lib/docker/overlay2/abc/diff")

        deployment._setup_rootfs_quota_xfs()

        assert deployment._rootfs_xfs_prjid is None
        assert deployment._rootfs_xfs_mountpoint is None

    @patch("rock.deployments.docker.subprocess.run")
    def test_rootfs_xfs_fallback_degrades_when_no_upper_dir(self, mock_run):
        """When UpperDir cannot be found, should degrade gracefully."""
        deployment = self._make_deployment()

        # docker inspect returns empty / <no value>
        mock_run.side_effect = [
            _make_subprocess_result(stdout="<no value>"),
            # docker inspect for container id (strategy 2)
            _make_subprocess_result(returncode=1),
        ]

        deployment._setup_rootfs_quota_xfs()

        assert deployment._rootfs_xfs_prjid is None


class TestCleanupRootfsXfsQuota:
    """Tests for DockerDeployment._cleanup_rootfs_xfs_quota()."""

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.subprocess.run")
    @patch("rock.deployments.docker.env_vars")
    def test_cleanup_calls_xfs_quota(self, mock_env, mock_run, _mock_validator):
        """Stop should invoke xfs_quota to clear limit and unbind project."""
        mock_env.ROCK_LOGGING_PATH = "/var/log/rock"
        mock_env.ROCK_WORKER_ENV_TYPE = "docker"
        mock_env.ROCK_TIME_ZONE = "UTC"
        mock_run.return_value = _make_subprocess_result()

        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk="50g"))
        deployment.set_container_name("test-container-xyz")
        deployment._rootfs_xfs_prjid = 2147483700
        deployment._rootfs_xfs_mountpoint = "/data"
        deployment._rootfs_upper_dir = "/data/docker/overlay2/abc/diff"

        deployment._cleanup_rootfs_xfs_quota()

        assert deployment._rootfs_xfs_prjid is None
        assert deployment._rootfs_xfs_mountpoint is None
        assert deployment._rootfs_upper_dir is None

        xfs_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "xfs_quota"]
        assert len(xfs_calls) == 3

    @patch("rock.deployments.docker.DockerSandboxValidator")
    def test_cleanup_noop_when_no_prjid(self, _mock_validator):
        """When no XFS quota was set, cleanup should be a no-op."""
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk="50g"))
        deployment.set_container_name("test-noop")
        assert deployment._rootfs_xfs_prjid is None
        deployment._cleanup_rootfs_xfs_quota()


class TestGetContainerUpperDir:
    """Tests for DockerDeployment._get_container_upper_dir()."""

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.subprocess.run")
    def test_strategy1_docker_inspect(self, mock_run, _mock_validator):
        """Should return UpperDir from docker inspect when available."""
        deployment = DockerDeployment.from_config(DockerDeploymentConfig())
        deployment.set_container_name("test-c1")

        mock_run.return_value = _make_subprocess_result(stdout="/var/lib/docker/overlay2/abc/diff")
        result = deployment._get_container_upper_dir()

        assert result == "/var/lib/docker/overlay2/abc/diff"

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.subprocess.run")
    @patch("builtins.open")
    def test_strategy2_proc_mounts(self, mock_open, mock_run, _mock_validator):
        """Should fall back to /proc/mounts when docker inspect returns <no value>."""
        deployment = DockerDeployment.from_config(DockerDeploymentConfig())
        deployment.set_container_name("test-c2")

        container_id = "abcdef1234567890"
        mock_run.side_effect = [
            # docker inspect UpperDir → <no value>
            _make_subprocess_result(stdout="<no value>"),
            # docker inspect Id
            _make_subprocess_result(stdout=container_id),
        ]
        mount_line = (
            f"overlay /run/containerd/io.containerd.runtime.v2/moby/{container_id}/rootfs "
            f"overlay rw,lowerdir=/x,upperdir=/data/snapshots/{container_id}/fs,workdir=/w 0 0\n"
        )
        mock_open.return_value.__enter__ = lambda s: iter([mount_line])
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = deployment._get_container_upper_dir()
        assert result == f"/data/snapshots/{container_id}/fs"

    @patch("rock.deployments.docker.DockerSandboxValidator")
    @patch("rock.deployments.docker.subprocess.run")
    def test_returns_none_when_both_fail(self, mock_run, _mock_validator):
        """Should return None when both strategies fail."""
        deployment = DockerDeployment.from_config(DockerDeploymentConfig())
        deployment.set_container_name("test-c3")

        mock_run.side_effect = [
            _make_subprocess_result(stdout="<no value>"),
            _make_subprocess_result(returncode=1),
        ]

        result = deployment._get_container_upper_dir()
        assert result is None
