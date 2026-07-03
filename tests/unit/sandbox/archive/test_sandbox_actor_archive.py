"""Unit tests for SandboxActor.archive() method (mocked, no Ray)."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_exit_actor():
    with patch("ray.actor.exit_actor"):
        yield


@pytest.fixture
def actor():
    """Create a SandboxActor instance without Ray, using the underlying class."""
    from rock.sandbox.sandbox_actor import SandboxActor

    ActorClass = SandboxActor.__ray_actor_class__

    config = MagicMock()
    config.container_name = "sbx-test-123"
    deployment = MagicMock()
    deployment.delete = AsyncMock()

    instance = ActorClass.__new__(ActorClass)
    instance._config = config
    instance._deployment = deployment
    instance._run_shell_command = AsyncMock()
    instance._archive_status = None
    return instance


@pytest.fixture
def dir_storage_config():
    return {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "test-bucket",
        "access_key_id": "test-ak",
        "access_key_secret": "test-sk",
        "region": "cn-hangzhou",
    }


@pytest.fixture
def image_storage_config():
    return {
        "registry_url": "rock-registry.cn-shanghai.cr.aliyuncs.com",
        "username": "test-user",
        "password": "test-pass",
    }


class TestSandboxActorArchive:
    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_happy_path(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", tmpdir)
            log_dir = os.path.join(tmpdir, "sbx-test-123")
            os.makedirs(log_dir)

            await actor.archive(dir_storage_config, image_storage_config)

        actor._run_shell_command.assert_any_call(
            "docker", "commit", "sbx-test-123", "archive-staging-sbx-test-123:latest"
        )
        mock_push.assert_called_once()
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        assert "sbx-test-123" in call_args[0][0]
        assert "sbx-test-123" in call_args[0][1]
        assert actor._archive_status.phases["image_archive"].status.value == "success"
        assert actor._archive_status.phases["log_archive"].status.value == "success"

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch(
        "rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local",
        new_callable=AsyncMock,
        side_effect=RuntimeError("push failed"),
    )
    async def test_push_failure_cleans_local_tag(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        await actor.archive(dir_storage_config, image_storage_config)

        actor._run_shell_command.assert_any_call("docker", "rmi", "archive-staging-sbx-test-123:latest", check=False)
        mock_upload.assert_not_called()
        assert actor._archive_status.phases["image_archive"].status.value == "failed"

    @patch(
        "rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upload failed"),
    )
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.delete", new_callable=AsyncMock)
    async def test_upload_failure_rolls_back_image(
        self, mock_delete_img, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", tmpdir)
            log_dir = os.path.join(tmpdir, "sbx-test-123")
            os.makedirs(log_dir)

            await actor.archive(dir_storage_config, image_storage_config)

        # Dir upload happens before image push; failure aborts before push.
        mock_push.assert_not_called()
        # Local tag still cleaned up
        actor._run_shell_command.assert_any_call("docker", "rmi", "archive-staging-sbx-test-123:latest", check=False)
        assert actor._archive_status.phases["log_archive"].status.value == "failed"

    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_no_logging_path_skips_log_archive(
        self, mock_push, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", None)

        await actor.archive(dir_storage_config, image_storage_config)

        mock_push.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_missing_log_dir_skips_upload(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/nonexistent")

        await actor.archive(dir_storage_config, image_storage_config)

        mock_push.assert_called_once()
        mock_upload.assert_not_called()


class TestArchiveSizeLimits:
    """Tests for archive size limit enforcement."""

    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_image_exceeds_limit_fails(
        self, mock_push, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import subprocess

        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", None)

        inspect_result = subprocess.CompletedProcess(args=(), returncode=0, stdout=b"20000000000", stderr=b"")
        actor._run_shell_command = AsyncMock(return_value=inspect_result)

        limits = {"max_image_push_size": "16g", "max_dir_upload_size": "16g"}

        await actor.archive(dir_storage_config, image_storage_config, limits)

        mock_push.assert_not_called()
        assert actor._archive_status.phases["image_archive"].status.value == "failed"

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_image_within_limit_proceeds(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import subprocess

        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", None)

        inspect_result = subprocess.CompletedProcess(args=(), returncode=0, stdout=b"1000000000", stderr=b"")
        actor._run_shell_command = AsyncMock(return_value=inspect_result)

        limits = {"max_image_push_size": "16g", "max_dir_upload_size": "16g"}
        await actor.archive(dir_storage_config, image_storage_config, limits)

        mock_push.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_dir_exceeds_limit_fails(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import subprocess

        import rock.env_vars as _env_vars

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", tmpdir)
            log_dir = os.path.join(tmpdir, "sbx-test-123")
            os.makedirs(log_dir)

            image_result = subprocess.CompletedProcess(args=(), returncode=0, stdout=b"1000000000", stderr=b"")
            du_result = subprocess.CompletedProcess(args=(), returncode=0, stdout=b"20000000000\t/tmp/logs", stderr=b"")

            async def side_effect(*args, **kwargs):
                if args[0] == "du":
                    return du_result
                return image_result

            actor._run_shell_command = AsyncMock(side_effect=side_effect)

            limits = {"max_image_push_size": "16g", "max_dir_upload_size": "16g"}
            await actor.archive(dir_storage_config, image_storage_config, limits)

        mock_push.assert_not_called()
        mock_upload.assert_not_called()
        assert actor._archive_status.phases["image_archive"].status.value == "failed"

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_no_limits_skips_checks(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", tmpdir)
            log_dir = os.path.join(tmpdir, "sbx-test-123")
            os.makedirs(log_dir)

            await actor.archive(dir_storage_config, image_storage_config)

        mock_push.assert_called_once()
        mock_upload.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.upload_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.push_from_local", new_callable=AsyncMock)
    async def test_empty_limit_string_skips_check(
        self, mock_push, mock_upload, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", tmpdir)
            log_dir = os.path.join(tmpdir, "sbx-test-123")
            os.makedirs(log_dir)

            limits = {"max_image_push_size": "", "max_dir_upload_size": ""}
            await actor.archive(dir_storage_config, image_storage_config, limits)

        mock_push.assert_called_once()
        mock_upload.assert_called_once()
