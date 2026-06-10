import pytest

from rock.sandbox.archive.abstract import AbstractImageStorage


class TestAbstractImageStorageCannotInstantiate:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AbstractImageStorage()


class InMemoryFakeImageStorage(AbstractImageStorage):
    """In-memory fake for testing callers of AbstractImageStorage."""

    def __init__(self, registry_url: str = "localhost:5000"):
        self._registry_url = registry_url
        self._store: dict[str, bytes] = {}

    @property
    def registry_url(self) -> str:
        return self._registry_url

    async def push_from_local(self, local_image_tag: str, remote_image_ref: str) -> None:
        self._store[remote_image_ref] = b"fake-manifest"

    async def pull_to_local(self, remote_image_ref: str) -> None:
        if remote_image_ref not in self._store:
            raise RuntimeError(f"image not found: {remote_image_ref}")

    async def delete(self, image_ref: str) -> bool:
        if image_ref in self._store:
            del self._store[image_ref]
            return True
        return False

    async def exists(self, image_ref: str) -> bool:
        return image_ref in self._store


class TestInMemoryFakeImageStorage:
    @pytest.fixture
    def storage(self):
        return InMemoryFakeImageStorage()

    async def test_push_then_exists(self, storage):
        await storage.push_from_local("local:tag", "localhost:5000/foo:bar")
        assert await storage.exists("localhost:5000/foo:bar") is True

    async def test_pull_nonexistent_raises(self, storage):
        with pytest.raises(RuntimeError):
            await storage.pull_to_local("localhost:5000/no:thing")

    async def test_delete(self, storage):
        await storage.push_from_local("local:tag", "localhost:5000/foo:bar")
        assert await storage.delete("localhost:5000/foo:bar") is True
        assert await storage.exists("localhost:5000/foo:bar") is False

    async def test_delete_nonexistent(self, storage):
        assert await storage.delete("nope") is False

    async def test_registry_url_property(self, storage):
        assert storage.registry_url == "localhost:5000"


class TestDockerRegistryV2ImageStorageWithAuth:
    """Test DockerRegistryV2ImageStorage with username/password (authenticated mode)."""

    def test_instantiates_with_auth(self):
        from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage

        s = DockerRegistryV2ImageStorage(
            registry_url="rock-registry.cn-shanghai.cr.aliyuncs.com",
            username="user",
            password="pass",
        )
        assert isinstance(s, AbstractImageStorage)

    def test_client_config_with_auth(self):
        from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage

        s = DockerRegistryV2ImageStorage(
            registry_url="rock-registry.cn-shanghai.cr.aliyuncs.com",
            username="user",
            password="pass",
        )
        cfg = s.client_config
        assert cfg["registry_url"] == "rock-registry.cn-shanghai.cr.aliyuncs.com"
        assert cfg["username"] == "user"
        assert cfg["password"] == "pass"

    def test_registry_url_property(self):
        from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage

        s = DockerRegistryV2ImageStorage(
            registry_url="rock-registry.cn-shanghai.cr.aliyuncs.com",
            username="user",
            password="pass",
        )
        assert s.registry_url == "rock-registry.cn-shanghai.cr.aliyuncs.com"

    async def test_delete_returns_false_when_challenge_fails(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage

        s = DockerRegistryV2ImageStorage(registry_url="r.example.com", username="u", password="p")
        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.status_code = 401
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=mock_client):
            assert await s.delete("r.example.com/ns/repo:tag") is False
