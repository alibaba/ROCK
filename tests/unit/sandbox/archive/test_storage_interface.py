import pytest

from rock.sandbox.archive.abstract import AbstractDirStorage


class TestAbstractDirStorageCannotInstantiate:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AbstractDirStorage()


class InMemoryFakeDirStorage(AbstractDirStorage):
    """In-memory fake for testing callers of AbstractDirStorage."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    async def upload_dir(self, local_dir: str, key: str) -> None:
        self._store[key] = b"fake-tar-content"

    async def download_to_dir(self, key: str, local_dir: str) -> None:
        if key not in self._store:
            raise FileNotFoundError(key)

    async def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def exists(self, key: str) -> bool:
        return key in self._store


class TestInMemoryFakeDirStorage:
    @pytest.fixture
    def storage(self):
        return InMemoryFakeDirStorage()

    async def test_upload_then_exists(self, storage):
        await storage.upload_dir("/tmp/fake", "mykey")
        assert await storage.exists("mykey") is True

    async def test_delete(self, storage):
        await storage.upload_dir("/tmp/fake", "mykey")
        assert await storage.delete("mykey") is True
        assert await storage.exists("mykey") is False

    async def test_delete_nonexistent(self, storage):
        assert await storage.delete("nope") is False

    async def test_download_nonexistent_raises(self, storage):
        with pytest.raises(FileNotFoundError):
            await storage.download_to_dir("nope", "/tmp/out")


class TestOssDirStorageInterface:
    def test_instantiates(self):
        from rock.sandbox.archive.oss_storage import OssDirStorage

        s = OssDirStorage(
            endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            bucket="test-bucket",
            access_key_id="ak",
            access_key_secret="sk",
            region="cn-hangzhou",
        )
        assert isinstance(s, AbstractDirStorage)

    def test_client_config(self):
        from rock.sandbox.archive.oss_storage import OssDirStorage

        s = OssDirStorage(
            endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            bucket="test-bucket",
            access_key_id="ak",
            access_key_secret="sk",
            region="cn-hangzhou",
        )
        cfg = s.client_config
        assert cfg["endpoint"] == "https://oss-cn-hangzhou.aliyuncs.com"
        assert cfg["bucket"] == "test-bucket"
        assert cfg["access_key_id"] == "ak"
        assert cfg["region"] == "cn-hangzhou"
