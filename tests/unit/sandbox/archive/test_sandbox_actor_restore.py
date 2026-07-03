"""Unit tests for SandboxActor.restore() method (mocked, no Ray)."""

from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from rock.deployments.docker import DockerDeployment


@pytest.fixture(autouse=True)
def mock_exit_actor():
    with patch("ray.actor.exit_actor") as m:
        yield m


@pytest.fixture
def actor():
    from rock.sandbox.sandbox_actor import SandboxActor

    ActorClass = SandboxActor.__ray_actor_class__

    config = MagicMock()
    config.container_name = "sbx-test-123"
    deployment = create_autospec(DockerDeployment, instance=True)
    deployment.restart = AsyncMock()
    deployment.restart_from_image = AsyncMock()

    instance = ActorClass.__new__(ActorClass)
    instance._config = config
    instance._deployment = deployment
    instance._run_shell_command = AsyncMock()
    instance._clean_container_background = MagicMock()
    instance._setup_monitor = AsyncMock()
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


class TestSandboxActorRestore:
    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.exists", new_callable=AsyncMock, return_value=True)
    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.download_to_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local", new_callable=AsyncMock)
    async def test_happy_path(
        self, mock_pull, mock_download, mock_exists, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        await actor.restore_and_start(dir_storage_config, image_storage_config)

        mock_pull.assert_called_once()
        ref_arg = mock_pull.call_args[0][0]
        assert "sbx-test-123" in ref_arg

        mock_download.assert_called_once()
        key_arg = mock_download.call_args[0][0]
        dir_arg = mock_download.call_args[0][1]
        assert "sbx-test-123" in key_arg
        assert dir_arg == "/tmp/logs/sbx-test-123"

        actor._deployment.restart_from_image.assert_called_once_with(ref_arg)
        actor._deployment.restart.assert_not_called()

    @patch(
        "rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local",
        new_callable=AsyncMock,
        side_effect=RuntimeError("pull failed"),
    )
    async def test_pull_failure_no_download(
        self, mock_pull, actor, dir_storage_config, image_storage_config, monkeypatch, mock_exit_actor
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        # restore_and_start catches the exception, logs it, then exits the actor
        # so a stale detached actor doesn't linger.  It should NOT re-raise.
        await actor.restore_and_start(dir_storage_config, image_storage_config)

        actor._run_shell_command.assert_not_called()
        mock_exit_actor.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.exists", new_callable=AsyncMock, return_value=True)
    @patch(
        "rock.sandbox.archive.oss_storage.OssDirStorage.download_to_dir",
        new_callable=AsyncMock,
        side_effect=RuntimeError("download failed"),
    )
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local", new_callable=AsyncMock)
    async def test_download_failure_warns_and_continues(
        self, mock_pull, mock_download, mock_exists, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        await actor.restore_and_start(dir_storage_config, image_storage_config)

        mock_pull.assert_called_once()
        actor._deployment.restart_from_image.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.exists", new_callable=AsyncMock, return_value=True)
    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.download_to_dir", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local", new_callable=AsyncMock)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.delete", new_callable=AsyncMock)
    async def test_restore_does_not_delete_remote(
        self,
        mock_delete,
        mock_pull,
        mock_download,
        mock_exists,
        actor,
        dir_storage_config,
        image_storage_config,
        monkeypatch,
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        await actor.restore_and_start(dir_storage_config, image_storage_config)
        mock_delete.assert_not_called()

    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local", new_callable=AsyncMock)
    async def test_no_logging_path_skips_log_restore(
        self, mock_pull, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", None)

        await actor.restore_and_start(dir_storage_config, image_storage_config)

        mock_pull.assert_called_once()
        actor._deployment.restart_from_image.assert_called_once()

    @patch("rock.sandbox.archive.oss_storage.OssDirStorage.exists", new_callable=AsyncMock, return_value=False)
    @patch("rock.sandbox.archive.registry_v2.DockerRegistryV2ImageStorage.pull_to_local", new_callable=AsyncMock)
    async def test_missing_key_warns_and_continues(
        self, mock_pull, mock_exists, actor, dir_storage_config, image_storage_config, monkeypatch
    ):
        import rock.env_vars as _env_vars

        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", "/tmp/logs")

        await actor.restore_and_start(dir_storage_config, image_storage_config)

        mock_pull.assert_called_once()
        actor._deployment.restart_from_image.assert_called_once()
