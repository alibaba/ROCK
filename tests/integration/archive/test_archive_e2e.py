"""E2E integration tests for archive + restore + delete full cycle.

Uses real MinIO + Docker Registry + a real docker container.
Does NOT require Ray — instantiates the underlying SandboxActor class directly.
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.sandbox.archive.constants import ArchiveKeys
from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage
from rock.sandbox.archive.s3_storage import S3DirStorage

pytestmark = [pytest.mark.integration]


@pytest.fixture
def sandbox_container():
    """Create a real docker container for archive testing."""
    container_name = f"test-archive-e2e-{os.getpid()}"
    subprocess.run(
        ["docker", "run", "-d", "--name", container_name, "busybox:latest", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    yield container_name
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


@pytest.fixture
def log_dir(sandbox_container):
    """Create a temp log dir with known content and return (path, file_hashes)."""
    base = tempfile.mkdtemp(prefix="rock-archive-e2e-logs-")
    log_path = Path(base) / sandbox_container
    log_path.mkdir(parents=True)

    hashes = {}
    for name, content in [("app.log", b"hello world\n" * 100), ("err.log", b"error line\n")]:
        p = log_path / name
        p.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()

    yield str(base), hashes

    import shutil

    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def dir_storage(local_minio):
    endpoint, access_key, secret_key, bucket = local_minio
    return S3DirStorage(
        endpoint=endpoint, bucket=bucket, access_key_id=access_key, access_key_secret=secret_key, region="us-east-1"
    )


@pytest.fixture
def image_storage(local_snapshot_registry):
    registry_url, username, password = local_snapshot_registry
    return DockerRegistryV2ImageStorage(registry_url=registry_url, username=username, password=password)


@pytest.fixture
def actor(sandbox_container):
    """Create SandboxActor instance (no Ray) using the underlying class."""
    from rock.sandbox.sandbox_actor import SandboxActor

    ActorClass = SandboxActor.__ray_actor_class__

    config = MagicMock()
    config.container_name = sandbox_container
    deployment = MagicMock()
    deployment.restart_from_image = AsyncMock()

    actor = ActorClass.__new__(ActorClass)
    actor._config = config
    actor._deployment = deployment
    actor._clean_container_background = MagicMock()
    actor._setup_monitor = AsyncMock()
    return actor


class TestArchiveRestoreDeleteE2E:
    """Full lifecycle: archive → verify exists → restore → verify restored → delete cleanup."""

    @pytest.fixture(autouse=True)
    def _patch_actor_storage(self, monkeypatch, log_dir):
        """Patch actor to use local S3 instead of OSS, and set ROCK_LOGGING_PATH."""
        import ray.actor

        import rock.sandbox.archive.oss_storage as oss_mod

        monkeypatch.setattr(oss_mod, "OssDirStorage", S3DirStorage)
        monkeypatch.setattr(ray.actor, "exit_actor", lambda: None)

        import rock.env_vars as _env_vars

        log_root, _ = log_dir
        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", log_root)

    async def test_full_cycle(self, actor, dir_storage, image_storage, log_dir, sandbox_container, local_minio):
        log_root, original_hashes = log_dir

        dir_cfg = dir_storage.client_config
        img_cfg = image_storage.client_config

        # === ARCHIVE ===
        await actor.archive(dir_cfg, img_cfg)

        key = ArchiveKeys.dir_key(sandbox_container, "rock-archives/")
        ref = ArchiveKeys.image_ref(sandbox_container, image_storage.registry_url, "sandbox_archive")

        assert await dir_storage.exists(key), "Archive dir object should exist in MinIO"
        assert await image_storage.exists(ref), "Archive image should exist in registry"

        # === SIMULATE "LOCAL IS GONE" ===
        log_path = Path(log_root) / sandbox_container
        import shutil

        shutil.rmtree(str(log_path), ignore_errors=True)
        assert not log_path.exists()

        local_tag = f"archive-staging-{sandbox_container}:latest"
        subprocess.run(["docker", "rmi", local_tag], capture_output=True)
        subprocess.run(["docker", "rmi", ref], capture_output=True)

        # === RESTORE ===
        await actor.restore_and_start(dir_cfg, img_cfg)

        assert log_path.exists(), "Log dir should be restored"
        for name, expected_hash in original_hashes.items():
            restored = (log_path / name).read_bytes()
            actual_hash = hashlib.sha256(restored).hexdigest()
            assert actual_hash == expected_hash, f"File {name} content mismatch after restore"

        result = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
        assert result.returncode == 0, "Image should be pullable locally after restore"

        # Remote still intact
        assert await dir_storage.exists(key), "Dir object should still exist after restore"
        assert await image_storage.exists(ref), "Image should still exist after restore"

        # === DELETE CLEANUP ===
        await dir_storage.delete(key)
        await image_storage.delete(ref)

        assert not await dir_storage.exists(key), "Dir object should be gone after delete"
        assert not await image_storage.exists(ref), "Image should be gone after delete"

    async def test_archive_rollback_on_upload_failure(
        self, actor, image_storage, sandbox_container, local_minio, monkeypatch
    ):
        """If dir upload fails, image should be rolled back."""
        import rock.env_vars as _env_vars

        dir_cfg = {
            "endpoint": "http://localhost:1",
            "bucket": "nonexistent",
            "access_key": "x",
            "secret_key": "x",
            "region": "us-east-1",
        }
        img_cfg = image_storage.client_config

        log_root = tempfile.mkdtemp(prefix="rock-archive-rollback-")
        monkeypatch.setattr(_env_vars, "ROCK_LOGGING_PATH", log_root)
        log_path = Path(log_root) / sandbox_container
        log_path.mkdir(parents=True)
        (log_path / "dummy.log").write_bytes(b"data")

        try:
            with pytest.raises(Exception):
                await actor._do_archive(dir_cfg, img_cfg, {})

            ref = ArchiveKeys.image_ref(sandbox_container, image_storage.registry_url, "sandbox_archive")
            assert not await image_storage.exists(ref), "Image should be cleaned up on failure"
        finally:
            import shutil

            shutil.rmtree(log_root, ignore_errors=True)


class TestIdempotentRestore:
    """Verify that restart_async with non-archived state does not trigger restore."""

    async def test_restart_async_rejects_running_state(self):
        """State machine guard: if state is RUNNING, raise error."""
        from rock.sandbox.sandbox_manager import SandboxManager

        m = MagicMock(spec=SandboxManager)
        m._operator = MagicMock()
        m._dir_storage = MagicMock()
        m._image_storage = MagicMock()

        sm = AsyncMock()
        sm.current_state.value = State.RUNNING
        sm.sandbox_info = {"sandbox_id": "sbx-x"}
        m._get_current_statemachine = AsyncMock(return_value=sm)

        m.restart_async = SandboxManager.restart_async.__get__(m, SandboxManager)

        from rock.sdk.common.exceptions import BadRequestRockError

        with pytest.raises(BadRequestRockError, match="cannot be restarted"):
            await m.restart_async("sbx-x")
