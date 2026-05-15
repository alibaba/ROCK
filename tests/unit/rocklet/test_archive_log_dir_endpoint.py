"""Tests for the rocklet /archive_log_dir endpoint."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rock.deployments.log_cleanup_sentinel import SentinelState, write_sentinel
from rock.rocklet.local_api import local_router


@pytest.fixture
def app():
    """Mount only the local_router (avoid full server setup)."""
    from fastapi import FastAPI

    a = FastAPI()
    a.include_router(local_router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "sandbox-test-container"
    d.mkdir()
    (d / "stdout.log").write_text("log content")
    return d


class TestArchiveLogDirEndpoint:
    def test_skipped_when_dir_missing(self, client, tmp_path):
        nonexistent = tmp_path / "does-not-exist"
        resp = client.post(
            "/archive_log_dir",
            json={"log_dir": str(nonexistent), "container_name": "ghost", "max_attempts": 3},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "skipped_no_sentinel"

    def test_skipped_when_no_sentinel(self, client, log_dir):
        """Dir exists but no sentinel → skipped_no_sentinel."""
        resp = client.post(
            "/archive_log_dir",
            json={"log_dir": str(log_dir), "container_name": log_dir.name, "max_attempts": 3},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "skipped_no_sentinel"

    def test_archived_on_successful_upload(self, client, log_dir):
        """Sentinel present + OssArchiver returns True → archived, dir removed."""
        write_sentinel(log_dir, SentinelState(stopped_at="2026-05-10T00:00:00+08:00", attempts=0))

        with patch("rock.rocklet.local_api.OssArchiver") as mock_archiver:
            mock_archiver.build_sandbox_log_key.return_value = "rock-archives/sandbox-logs/x.tar.gz"
            mock_archiver.try_upload_dir_sync.return_value = True

            resp = client.post(
                "/archive_log_dir",
                json={"log_dir": str(log_dir), "container_name": log_dir.name, "max_attempts": 3},
            )

        assert resp.status_code == 200
        assert resp.json()["outcome"] == "archived"
        assert not log_dir.exists()  # dir removed on worker

    def test_failed_pending_below_max(self, client, log_dir):
        """Upload fails, attempts < max → failed_pending, dir kept, sentinel bumped."""
        write_sentinel(log_dir, SentinelState(stopped_at="2026-05-10T00:00:00+08:00", attempts=0))

        with patch("rock.rocklet.local_api.OssArchiver") as mock_archiver:
            mock_archiver.build_sandbox_log_key.return_value = "rock-archives/sandbox-logs/x.tar.gz"
            mock_archiver.try_upload_dir_sync.return_value = False

            resp = client.post(
                "/archive_log_dir",
                json={"log_dir": str(log_dir), "container_name": log_dir.name, "max_attempts": 3},
            )

        body = resp.json()
        assert body["outcome"] == "failed_pending"
        assert body["attempts"] == 1
        assert log_dir.is_dir()  # dir preserved

    def test_failed_persist_at_max(self, client, log_dir):
        """Upload fails, attempts → max → failed_persist, dir kept (no rmtree)."""
        write_sentinel(log_dir, SentinelState(stopped_at="2026-05-10T00:00:00+08:00", attempts=2))

        with patch("rock.rocklet.local_api.OssArchiver") as mock_archiver:
            mock_archiver.build_sandbox_log_key.return_value = "rock-archives/sandbox-logs/x.tar.gz"
            mock_archiver.try_upload_dir_sync.return_value = False

            resp = client.post(
                "/archive_log_dir",
                json={"log_dir": str(log_dir), "container_name": log_dir.name, "max_attempts": 3},
            )

        body = resp.json()
        assert body["outcome"] == "failed_persist"
        assert body["attempts"] == 3
        assert log_dir.is_dir()  # dir preserved for FileCleanupTask
