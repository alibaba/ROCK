"""
Unit tests for anonymous volume (Dockerfile VOLUME) quota sharing and cleanup.

Tests cover:
- _setup_local_volumes_quota_shared(): binds anonymous volumes to rootfs prjid
- remove_container_force(): includes -v flag to clean up anonymous volumes
- ContainerCleanupTask: docker rm commands include -v flag
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.utils.docker import DockerUtil


def _make_run_result(returncode=0, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _make_deployment(disk_limit_rootfs="20g") -> DockerDeployment:
    with patch("rock.deployments.docker.DockerSandboxValidator"):
        deployment = DockerDeployment.from_config(DockerDeploymentConfig(disk=disk_limit_rootfs))
    deployment._container_name = "test-container"
    deployment._effective_disk = disk_limit_rootfs
    return deployment


MOCK_MOUNTS_WITH_VOLUMES = json.dumps(
    [
        {
            "Type": "volume",
            "Name": "abc123" * 10 + "abcd",
            "Source": "/var/lib/docker/volumes/abc123/_data",
            "Destination": "/data",
            "Driver": "local",
        },
        {
            "Type": "bind",
            "Source": "/tmp",
            "Destination": "/mnt/host-tmp",
            "Mode": "ro",
        },
        {
            "Type": "volume",
            "Name": "def456" * 10 + "defg",
            "Source": "/var/lib/docker/volumes/def456/_data",
            "Destination": "/cache",
            "Driver": "local",
        },
    ]
)

MOCK_MOUNTS_NO_VOLUMES = json.dumps(
    [
        {
            "Type": "bind",
            "Source": "/tmp",
            "Destination": "/mnt/host-tmp",
            "Mode": "ro",
        },
    ]
)


class TestSetupLocalVolumesQuotaShared:
    """Tests for DockerDeployment._setup_local_volumes_quota_shared()."""

    def test_skips_when_no_rootfs_limit(self):
        deployment = _make_deployment(disk_limit_rootfs=None)
        deployment._effective_disk = None
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            deployment._setup_local_volumes_quota_shared()
            mock_run.assert_not_called()

    def test_skips_when_prjid_unavailable(self):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(return_value=(None, None))
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            deployment._setup_local_volumes_quota_shared()
            mock_run.assert_not_called()

    def test_skips_when_no_anonymous_volumes(self):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )
        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value = _make_run_result(stdout=MOCK_MOUNTS_NO_VOLUMES)
            deployment._setup_local_volumes_quota_shared()
            assert mock_run.call_count == 1  # only docker inspect

    @patch("rock.deployments.docker.os.stat")
    @patch("rock.deployments.docker.subprocess.run")
    def test_binds_volumes_to_rootfs_prjid(self, mock_run, mock_stat):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )

        stat_result = MagicMock()
        stat_result.st_dev = 42
        mock_stat.return_value = stat_result

        mock_run.side_effect = [
            # docker inspect --format={{json .Mounts}}
            _make_run_result(stdout=MOCK_MOUNTS_WITH_VOLUMES),
            # findmnt
            _make_run_result(stdout="/data/docker"),
            # xfs_quota for first volume
            _make_run_result(returncode=0),
            # xfs_quota for second volume
            _make_run_result(returncode=0),
        ]

        deployment._setup_local_volumes_quota_shared()

        xfs_calls = [c for c in mock_run.call_args_list if "xfs_quota" in str(c)]
        assert len(xfs_calls) == 2
        assert "12345" in str(xfs_calls[0])
        assert "/var/lib/docker/volumes/abc123/_data" in str(xfs_calls[0])
        assert "/var/lib/docker/volumes/def456/_data" in str(xfs_calls[1])

    @patch("rock.deployments.docker.os.stat")
    @patch("rock.deployments.docker.subprocess.run")
    def test_skips_volume_on_different_filesystem(self, mock_run, mock_stat):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )

        call_count = [0]

        def stat_side_effect(path):
            result = MagicMock()
            call_count[0] += 1
            if path == "/var/lib/docker/overlay2/abc/diff":
                result.st_dev = 42
            else:
                result.st_dev = 99  # different filesystem
            return result

        mock_stat.side_effect = stat_side_effect

        mock_run.side_effect = [
            _make_run_result(stdout=MOCK_MOUNTS_WITH_VOLUMES),
            # findmnt
            _make_run_result(stdout="/data/docker"),
        ]

        deployment._setup_local_volumes_quota_shared()

        xfs_calls = [c for c in mock_run.call_args_list if "xfs_quota" in str(c)]
        assert len(xfs_calls) == 0

    @patch("rock.deployments.docker.os.stat")
    @patch("rock.deployments.docker.subprocess.run")
    def test_continues_on_xfs_quota_failure(self, mock_run, mock_stat):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )

        stat_result = MagicMock()
        stat_result.st_dev = 42
        mock_stat.return_value = stat_result

        mock_run.side_effect = [
            _make_run_result(stdout=MOCK_MOUNTS_WITH_VOLUMES),
            _make_run_result(stdout="/data/docker"),
            # first volume xfs_quota fails
            _make_run_result(returncode=1, stderr="quota error"),
            # second volume xfs_quota succeeds
            _make_run_result(returncode=0),
        ]

        deployment._setup_local_volumes_quota_shared()

        xfs_calls = [c for c in mock_run.call_args_list if "xfs_quota" in str(c)]
        assert len(xfs_calls) == 2

    def test_handles_inspect_failure(self):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )

        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.return_value = _make_run_result(returncode=1)
            deployment._setup_local_volumes_quota_shared()

    def test_handles_inspect_timeout(self):
        deployment = _make_deployment()
        deployment._get_docker_rootfs_prjid_and_upper_dir = MagicMock(
            return_value=(12345, "/var/lib/docker/overlay2/abc/diff")
        )

        with patch("rock.deployments.docker.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=5)
            deployment._setup_local_volumes_quota_shared()


class TestRemoveContainerForceIncludesV:
    """Verify remove_container_force uses -v to clean up anonymous volumes."""

    @patch("rock.utils.docker.subprocess.run")
    def test_rm_command_includes_v_flag(self, mock_run):
        DockerUtil.remove_container_force("test-container")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "rm", "-f", "-v", "test-container"]


class TestContainerCleanupTaskIncludesV:
    """Verify ContainerCleanupTask uses docker rm -v."""

    def test_cleanup_command_includes_v_flag(self):
        from rock.admin.scheduler.tasks.container_cleanup_task import ContainerCleanupTask

        task = ContainerCleanupTask(max_age_hours=24)
        # Access the command string built in run_action by inspecting the source
        import inspect

        source = inspect.getsource(task.run_action)
        assert "docker rm -v" in source
