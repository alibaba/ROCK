"""Tests for rock.utils.oss_archiver — OssArchiver tar+upload utility."""

from unittest.mock import MagicMock, patch

import pytest

from rock.utils.oss_archiver import OssArchiver


@pytest.fixture
def mock_rock_config():
    mock_cfg = MagicMock()
    mock_cfg.oss.archive_prefix = "rock-archives/"
    mock_cfg.oss.archive_ttl_days = 30
    mock_cfg.oss.primary.bucket = "chatos-rock"
    mock_cfg.oss.primary.endpoint = "oss-cn-hangzhou.aliyuncs.com"
    mock_cfg.oss.primary.access_key_id = "fake-ak"
    mock_cfg.oss.primary.access_key_secret = "fake-sk"
    return mock_cfg


@pytest.fixture
def sample_log_dir(tmp_path):
    """Create a sample log directory with files."""
    log_dir = tmp_path / "sandbox-test-container"
    log_dir.mkdir()
    (log_dir / "stdout.log").write_text("stdout content " * 100)
    (log_dir / "stderr.log").write_text("stderr content " * 50)
    sub = log_dir / "subdir"
    sub.mkdir()
    (sub / "nested.log").write_text("nested log content")
    return log_dir


class TestGetBucket:
    def test_returns_none_when_primary_bucket_empty(self, mock_rock_config):
        """oss.primary.bucket empty → _get_bucket() returns None."""
        mock_rock_config.oss.primary.bucket = ""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = OssArchiver._get_bucket()
            assert result is None

    def test_returns_bucket_when_configured(self, mock_rock_config):
        """oss.primary.bucket non-empty → returns oss2.Bucket instance."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_bucket = MagicMock()
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = mock_bucket

                result = OssArchiver._get_bucket()
                assert result is mock_bucket
                mock_oss2.Auth.assert_called_once_with("fake-ak", "fake-sk")
                mock_oss2.Bucket.assert_called_once()

    def test_returns_none_on_exception(self, mock_rock_config):
        """Exception during bucket init → returns None (fail-safe)."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.side_effect = Exception("config unavailable")
            result = OssArchiver._get_bucket()
            assert result is None


class TestBuildSandboxLogKey:
    def test_default_prefix(self, mock_rock_config):
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            key = OssArchiver.build_sandbox_log_key("my-container")
            assert key == "rock-archives/sandbox-logs/my-container.tar.gz"

    def test_custom_prefix(self, mock_rock_config):
        mock_rock_config.oss.archive_prefix = "custom-prefix"
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            key = OssArchiver.build_sandbox_log_key("abc")
            assert key == "custom-prefix/sandbox-logs/abc.tar.gz"


class TestTryUploadDirSync:
    def test_returns_false_when_bucket_unavailable(self, mock_rock_config, sample_log_dir):
        """Primary bucket empty → try_upload_dir_sync returns False."""
        mock_rock_config.oss.primary.bucket = ""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = OssArchiver.try_upload_dir_sync(
                str(sample_log_dir),
                "rock-archives/sandbox-logs/test.tar.gz",
            )
            assert result is False

    def test_returns_true_for_nonexistent_dir(self, mock_rock_config):
        """Non-existent dir → returns True (nothing to archive is success)."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = MagicMock()

                result = OssArchiver.try_upload_dir_sync(
                    "/nonexistent/path/xyz",
                    "rock-archives/sandbox-logs/test.tar.gz",
                )
                assert result is True

    def test_returns_true_for_empty_dir(self, mock_rock_config, tmp_path):
        """Empty dir → returns True (nothing to archive)."""
        empty_dir = tmp_path / "empty-sandbox"
        empty_dir.mkdir()
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = MagicMock()

                result = OssArchiver.try_upload_dir_sync(
                    str(empty_dir),
                    "rock-archives/sandbox-logs/test.tar.gz",
                )
                assert result is True

    def test_returns_false_when_size_exceeds_limit(self, mock_rock_config, sample_log_dir):
        """Dir size > max_size_bytes → returns False."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = MagicMock()

                result = OssArchiver.try_upload_dir_sync(
                    str(sample_log_dir),
                    "rock-archives/sandbox-logs/test.tar.gz",
                    max_size_bytes=1,  # 1 byte limit
                )
                assert result is False

    def test_successful_upload(self, mock_rock_config, sample_log_dir):
        """Normal upload → put_object called + returns True."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_bucket = MagicMock()
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = mock_bucket

                result = OssArchiver.try_upload_dir_sync(
                    str(sample_log_dir),
                    "rock-archives/sandbox-logs/test.tar.gz",
                )
                assert result is True
                mock_bucket.put_object.assert_called_once()

    def test_returns_false_on_oss_exception(self, mock_rock_config, sample_log_dir):
        """OSS put_object raises → returns False."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_bucket = MagicMock()
                mock_bucket.put_object.side_effect = Exception("network error")
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = mock_bucket

                result = OssArchiver.try_upload_dir_sync(
                    str(sample_log_dir),
                    "rock-archives/sandbox-logs/test.tar.gz",
                )
                assert result is False

    def test_no_class_singleton_each_call_rebuilds_bucket(self, mock_rock_config, sample_log_dir):
        """Verify _get_bucket is called each time (no class-level cache)."""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = MagicMock()

                OssArchiver.try_upload_dir_sync(str(sample_log_dir), "key1.tar.gz")
                OssArchiver.try_upload_dir_sync(str(sample_log_dir), "key2.tar.gz")

                # oss2.Bucket should be called at least twice (once per call)
                assert mock_oss2.Bucket.call_count >= 2


class TestGetObject:
    @pytest.mark.asyncio
    async def test_returns_false_when_bucket_unavailable(self, mock_rock_config):
        mock_rock_config.oss.primary.bucket = ""
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            result = await OssArchiver.get_object("some/key.tar.gz", "/tmp/out.tar.gz")
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, mock_rock_config, tmp_path):
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_bucket = MagicMock()
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = mock_bucket

                out_path = str(tmp_path / "output.tar.gz")
                result = await OssArchiver.get_object("rock-archives/sandbox-logs/x.tar.gz", out_path)
                assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_download_error(self, mock_rock_config, tmp_path):
        with patch("rock.utils.oss_archiver.RockConfig") as mock_rc:
            mock_rc.from_env.return_value = mock_rock_config
            with patch("rock.utils.oss_archiver.oss2") as mock_oss2:
                mock_bucket = MagicMock()
                mock_bucket.get_object_to_file.side_effect = Exception("download failed")
                mock_oss2.Auth.return_value = MagicMock()
                mock_oss2.Bucket.return_value = mock_bucket

                out_path = str(tmp_path / "output.tar.gz")
                result = await OssArchiver.get_object("some/key.tar.gz", out_path)
                assert result is False
