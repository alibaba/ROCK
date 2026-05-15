"""Tests for DockerDeployment._handle_sandbox_log_dir — log cleanup policy dispatch on stop."""

from unittest.mock import patch

import pytest

from rock.deployments.log_cleanup import LogCleanupPolicy
from rock.deployments.log_cleanup_sentinel import read_sentinel, sentinel_path


@pytest.fixture
def log_dir(tmp_path):
    """Simulated sandbox log directory with some files."""
    d = tmp_path / "sandbox-container-xyz"
    d.mkdir()
    (d / "stdout.log").write_text("some log output")
    (d / "stderr.log").write_text("error output")
    return d


@pytest.fixture
def docker_deployment():
    """Minimal mock of DockerDeployment with _handle_sandbox_log_dir accessible."""
    from rock.deployments.docker import DockerDeployment

    # We test _handle_sandbox_log_dir directly as it's the public interface
    # for policy dispatch. Creating a full DockerDeployment requires too much
    # infrastructure; instead we instantiate minimally and call the method.
    deployment = object.__new__(DockerDeployment)
    return deployment


class TestKeepPolicy:
    def test_keep_does_not_modify_directory(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP)
        # Directory and contents remain untouched
        assert log_dir.is_dir()
        assert (log_dir / "stdout.log").is_file()
        assert (log_dir / "stderr.log").is_file()

    def test_keep_does_not_write_sentinel(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP)
        assert not sentinel_path(log_dir).exists()


class TestCleanDirectlyPolicy:
    def test_removes_directory(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.CLEAN_DIRECTLY)
        assert not log_dir.exists()

    def test_handles_already_removed_dir(self, docker_deployment, tmp_path):
        """shutil.rmtree with ignore_errors=True should not raise."""
        missing_dir = tmp_path / "already-gone"
        missing_dir.mkdir()
        missing_dir.rmdir()  # remove it before calling
        # Should not raise
        docker_deployment._handle_sandbox_log_dir(missing_dir, LogCleanupPolicy.CLEAN_DIRECTLY)


class TestKeepThenArchivePolicy:
    def test_writes_sentinel_file(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
        assert sentinel_path(log_dir).exists()

    def test_sentinel_has_correct_fields(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
        state = read_sentinel(log_dir)
        assert state is not None
        assert state.attempts == 0
        assert state.version == 1
        assert state.stopped_at  # non-empty

    def test_does_not_call_oss_archiver(self, docker_deployment, log_dir):
        """v5 key design: _stop() only writes sentinel, NEVER uploads."""
        with patch("rock.utils.oss_archiver.OssArchiver.try_upload_dir_sync") as mock_upload:
            docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
            mock_upload.assert_not_called()

    def test_directory_remains_intact(self, docker_deployment, log_dir):
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
        assert log_dir.is_dir()
        assert (log_dir / "stdout.log").is_file()

    def test_idempotent_no_overwrite_on_second_call(self, docker_deployment, log_dir):
        """Second stop call must not reset stopped_at timestamp."""
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
        state1 = read_sentinel(log_dir)

        # Simulate second _stop() call
        docker_deployment._handle_sandbox_log_dir(log_dir, LogCleanupPolicy.KEEP_THEN_ARCHIVE)
        state2 = read_sentinel(log_dir)

        assert state1.stopped_at == state2.stopped_at
        assert state1.attempts == state2.attempts
