"""
Unit tests for DockerUtil.detect_storage_opt_support().

All subprocess calls are mocked so no Docker daemon is required.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rock.utils.docker import DockerUtil


# ---- helpers ----


def _make_run_result(returncode=0, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _docker_info_json(driver="overlay2", backing_fs="xfs", docker_root="/var/lib/docker"):
    """Return a JSON string mimicking `docker info --format '{{json .}}'`."""
    info = {
        "Driver": driver,
        "DriverStatus": [
            ["Backing Filesystem", backing_fs],
            ["Supports d_type", "true"],
        ],
        "DockerRootDir": docker_root,
    }
    return json.dumps(info)


def _containerd_info_json(docker_root="/var/lib/docker"):
    """Return a JSON string for containerd image store (snapshotter)."""
    info = {
        "Driver": "overlayfs",
        "DriverStatus": [
            ["driver-type", "io.containerd.snapshotter.v1"],
        ],
        "DockerRootDir": docker_root,
    }
    return json.dumps(info)


# ---- detect_storage_opt_support tests ----


class TestDetectStorageOptSupport:
    """Tests for DockerUtil.detect_storage_opt_support()."""

    # findmnt now returns "FSTYPE OPTIONS" (delegated to is_xfs_prjquota_path)
    @patch("rock.utils.docker.subprocess.run")
    def test_all_requirements_met_prjquota(self, mock_run):
        """Should return True when overlay2 + xfs + prjquota are all present."""
        mock_run.side_effect = [
            # docker info
            _make_run_result(stdout=_docker_info_json()),
            # findmnt for DockerRootDir (FSTYPE OPTIONS)
            _make_run_result(stdout="xfs rw,relatime,attr2,inode64,prjquota"),
        ]
        assert DockerUtil.detect_storage_opt_support() is True

    @patch("rock.utils.docker.subprocess.run")
    def test_all_requirements_met_pquota(self, mock_run):
        """Should return True when pquota (synonym for prjquota) is present."""
        mock_run.side_effect = [
            _make_run_result(stdout=_docker_info_json()),
            _make_run_result(stdout="xfs rw,relatime,attr2,inode64,pquota"),
        ]
        assert DockerUtil.detect_storage_opt_support() is True

    @patch("rock.utils.docker.subprocess.run")
    def test_non_overlay2_driver(self, mock_run):
        """Should return False if the storage driver is not overlay2."""
        mock_run.return_value = _make_run_result(stdout=_docker_info_json(driver="aufs"))
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_non_xfs_docker_root(self, mock_run):
        """Should return False if DockerRootDir is not on XFS."""
        mock_run.side_effect = [
            _make_run_result(stdout=_docker_info_json()),
            _make_run_result(stdout="ext4 rw,relatime"),
        ]
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_missing_prjquota(self, mock_run):
        """Should return False when mount options lack prjquota/pquota."""
        mock_run.side_effect = [
            _make_run_result(stdout=_docker_info_json()),
            _make_run_result(stdout="xfs rw,relatime,attr2,inode64,noquota"),
        ]
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_docker_info_fails(self, mock_run):
        """Should return False if docker info returns non-zero exit code."""
        mock_run.return_value = _make_run_result(returncode=1, stderr="Cannot connect to Docker daemon")
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_docker_info_raises_exception(self, mock_run):
        """Should return False if docker info subprocess raises."""
        mock_run.side_effect = FileNotFoundError("docker not found")
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_findmnt_fails(self, mock_run):
        """Should return False if findmnt returns non-zero exit code."""
        mock_run.side_effect = [
            _make_run_result(stdout=_docker_info_json()),
            _make_run_result(returncode=1, stderr="findmnt: failed"),
        ]
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_findmnt_raises_exception(self, mock_run):
        """Should return False if findmnt subprocess raises."""
        mock_run.side_effect = [
            _make_run_result(stdout=_docker_info_json()),
            subprocess.TimeoutExpired(cmd="findmnt", timeout=5),
        ]
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_missing_docker_root_dir(self, mock_run):
        """Should return False if DockerRootDir is missing from docker info."""
        info = {"Driver": "overlay2", "DriverStatus": [["Backing Filesystem", "xfs"]]}
        mock_run.return_value = _make_run_result(stdout=json.dumps(info))
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_containerd_image_store_xfs_prjquota(self, mock_run):
        """Should return True for containerd image store on xfs+prjquota."""
        mock_run.side_effect = [
            _make_run_result(stdout=_containerd_info_json()),
            _make_run_result(stdout="xfs rw,relatime,attr2,inode64,prjquota"),
        ]
        assert DockerUtil.detect_storage_opt_support() is True

    @patch("rock.utils.docker.subprocess.run")
    def test_containerd_image_store_no_prjquota(self, mock_run):
        """Should return False for containerd image store without prjquota."""
        mock_run.side_effect = [
            _make_run_result(stdout=_containerd_info_json()),
            _make_run_result(stdout="xfs rw,relatime,attr2,inode64,noquota"),
        ]
        assert DockerUtil.detect_storage_opt_support() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_containerd_image_store_non_xfs(self, mock_run):
        """Should return False for containerd image store on non-XFS."""
        mock_run.side_effect = [
            _make_run_result(stdout=_containerd_info_json()),
            _make_run_result(stdout="ext4 rw,relatime"),
        ]
        assert DockerUtil.detect_storage_opt_support() is False


# ---- is_xfs_prjquota_path tests ----


class TestIsXfsPrjquotaPath:
    """Tests for DockerUtil.is_xfs_prjquota_path().

    Unlike detect_storage_opt_support(), this check is path-local and has
    no dependency on Docker's storage driver.
    """

    @patch("rock.utils.docker.subprocess.run")
    def test_xfs_with_prjquota(self, mock_run):
        mock_run.return_value = _make_run_result(stdout="xfs rw,relatime,attr2,inode64,prjquota")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is True

    @patch("rock.utils.docker.subprocess.run")
    def test_xfs_with_pquota_synonym(self, mock_run):
        """pquota is a synonym for prjquota and must also be accepted."""
        mock_run.return_value = _make_run_result(stdout="xfs rw,relatime,attr2,inode64,pquota")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is True

    @patch("rock.utils.docker.subprocess.run")
    def test_xfs_without_prjquota(self, mock_run):
        """XFS mount without prjquota/pquota should return False."""
        mock_run.return_value = _make_run_result(stdout="xfs rw,relatime,attr2,inode64,noquota")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False

    @patch("rock.utils.docker.subprocess.run")
    def test_non_xfs_with_prjquota(self, mock_run):
        """ext4 with prjquota-like options should return False (not XFS)."""
        mock_run.return_value = _make_run_result(stdout="ext4 rw,relatime,prjquota")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False

    @patch("rock.utils.docker.subprocess.run")
    def test_findmnt_failure(self, mock_run):
        mock_run.return_value = _make_run_result(returncode=1, stderr="findmnt: failed")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False

    @patch("rock.utils.docker.subprocess.run")
    def test_findmnt_exception(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="findmnt", timeout=5)
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False

    @patch("rock.utils.docker.subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = _make_run_result(stdout="")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False

    @patch("rock.utils.docker.subprocess.run")
    def test_only_fstype_no_options(self, mock_run):
        """If findmnt returns only FSTYPE with no OPTIONS column, return False."""
        mock_run.return_value = _make_run_result(stdout="xfs")
        assert DockerUtil.is_xfs_prjquota_path("/data/logs") is False


# ---- detect_containerd_image_store tests ----


class TestDetectContainerdImageStore:
    """Tests for DockerUtil.detect_containerd_image_store()."""

    @patch("rock.utils.docker.subprocess.run")
    def test_containerd_snapshotter_detected(self, mock_run):
        """Should return True when DriverStatus contains containerd snapshotter driver-type."""
        info = {
            "Driver": "overlayfs",
            "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
            "DockerRootDir": "/var/lib/docker",
        }
        mock_run.return_value = _make_run_result(stdout=json.dumps(info))
        assert DockerUtil.detect_containerd_image_store() is True

    @patch("rock.utils.docker.subprocess.run")
    def test_overlay2_not_containerd(self, mock_run):
        """Should return False for classic overlay2 (no snapshotter driver-type)."""
        mock_run.return_value = _make_run_result(stdout=_docker_info_json(driver="overlay2"))
        assert DockerUtil.detect_containerd_image_store() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_docker_info_fails(self, mock_run):
        """Should return False when docker info is unavailable."""
        mock_run.return_value = _make_run_result(returncode=1, stderr="Cannot connect")
        assert DockerUtil.detect_containerd_image_store() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_no_driver_status(self, mock_run):
        """Should return False when DriverStatus is missing."""
        info = {"Driver": "overlayfs", "DockerRootDir": "/var/lib/docker"}
        mock_run.return_value = _make_run_result(stdout=json.dumps(info))
        assert DockerUtil.detect_containerd_image_store() is False

    @patch("rock.utils.docker.subprocess.run")
    def test_different_driver_type(self, mock_run):
        """Should return False when driver-type is not containerd snapshotter."""
        info = {
            "Driver": "overlay2",
            "DriverStatus": [["driver-type", "io.docker.storage.v1"]],
            "DockerRootDir": "/var/lib/docker",
        }
        mock_run.return_value = _make_run_result(stdout=json.dumps(info))
        assert DockerUtil.detect_containerd_image_store() is False
