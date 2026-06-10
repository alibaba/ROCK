import os
import tempfile

import pytest

from rock.sandbox.archive.s3_storage import S3DirStorage


@pytest.fixture
def s3_storage(local_minio):
    endpoint, access_key, secret_key, bucket = local_minio
    return S3DirStorage(
        endpoint=endpoint,
        bucket=bucket,
        access_key_id=access_key,
        access_key_secret=secret_key,
        region="us-east-1",
    )


@pytest.fixture
def sample_dir():
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "mydata")
        os.makedirs(d)
        with open(os.path.join(d, "file1.txt"), "w") as f:
            f.write("hello world")
        os.makedirs(os.path.join(d, "subdir"))
        with open(os.path.join(d, "subdir", "file2.bin"), "wb") as f:
            f.write(b"\x00\x01\x02" * 100)
        yield d


class TestS3DirStorage:
    async def test_upload_exists_delete_cycle(self, s3_storage, sample_dir):
        key = "test/cycle.tar.gz"
        await s3_storage.upload_dir(sample_dir, key)
        assert await s3_storage.exists(key) is True

        assert await s3_storage.delete(key) is True
        assert await s3_storage.exists(key) is False

    async def test_delete_nonexistent(self, s3_storage):
        assert await s3_storage.delete("no/such/key") is False

    async def test_upload_idempotent(self, s3_storage, sample_dir):
        key = "test/idempotent.tar.gz"
        await s3_storage.upload_dir(sample_dir, key)
        await s3_storage.upload_dir(sample_dir, key)
        assert await s3_storage.exists(key) is True
        await s3_storage.delete(key)

    async def test_upload_download_roundtrip(self, s3_storage, sample_dir):
        key = "test/roundtrip.tar.gz"
        await s3_storage.upload_dir(sample_dir, key)

        with tempfile.TemporaryDirectory() as td:
            restore_dir = os.path.join(td, "mydata")
            await s3_storage.download_to_dir(key, restore_dir)

            assert os.path.isdir(restore_dir)
            with open(os.path.join(restore_dir, "file1.txt")) as f:
                assert f.read() == "hello world"
            with open(os.path.join(restore_dir, "subdir", "file2.bin"), "rb") as f:
                assert f.read() == b"\x00\x01\x02" * 100

        await s3_storage.delete(key)

    async def test_upload_nonexistent_dir_raises(self, s3_storage):
        with pytest.raises(FileNotFoundError):
            await s3_storage.upload_dir("/tmp/does-not-exist-xyz", "test/nope.tar.gz")

    async def test_download_target_exists_raises(self, s3_storage, sample_dir):
        key = "test/exists-check.tar.gz"
        await s3_storage.upload_dir(sample_dir, key)

        with pytest.raises(FileExistsError):
            await s3_storage.download_to_dir(key, sample_dir)

        await s3_storage.delete(key)

    async def test_download_missing_key_raises(self, s3_storage):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "output")
            with pytest.raises(FileNotFoundError):
                await s3_storage.download_to_dir("no/key", target)
            assert not os.path.exists(target)
