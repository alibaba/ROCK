import subprocess

import pytest

from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage


@pytest.fixture
def image_storage(local_snapshot_registry) -> DockerRegistryV2ImageStorage:
    registry_url, username, password = local_snapshot_registry
    return DockerRegistryV2ImageStorage(registry_url=registry_url, username=username, password=password)


@pytest.fixture(scope="session")
def test_container():
    """Create a busybox container for commit tests."""
    name = "test-archive-source"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "busybox:latest", "sleep", "3600"],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def local_image(test_container):
    """Commit the test container to a local image tag."""
    tag = "archive-test-local:latest"
    subprocess.run(["docker", "commit", test_container, tag], check=True, capture_output=True)
    yield tag
    subprocess.run(["docker", "rmi", tag], capture_output=True)


class TestDockerRegistryV2ImageStorage:
    async def test_push_then_exists(self, image_storage, local_image):
        ref = f"{image_storage.registry_url}/test-ns/myimg:v1"
        await image_storage.push_from_local(local_image, ref)
        assert await image_storage.exists(ref) is True
        await image_storage.delete(ref)

    async def test_push_pull_roundtrip(self, image_storage, local_image):
        ref = f"{image_storage.registry_url}/test-ns/roundtrip:v1"
        await image_storage.push_from_local(local_image, ref)

        subprocess.run(["docker", "rmi", ref], capture_output=True)

        await image_storage.pull_to_local(ref)
        result = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
        assert result.returncode == 0

        subprocess.run(["docker", "rmi", ref], capture_output=True)
        await image_storage.delete(ref)

    async def test_pull_nonexistent_raises(self, image_storage):
        ref = f"{image_storage.registry_url}/test-ns/nosuch:v999"
        with pytest.raises(RuntimeError):
            await image_storage.pull_to_local(ref)

    async def test_delete_then_not_exists(self, image_storage, local_image):
        ref = f"{image_storage.registry_url}/test-ns/todelete:v1"
        await image_storage.push_from_local(local_image, ref)
        assert await image_storage.delete(ref) is True
        assert await image_storage.exists(ref) is False

    async def test_delete_nonexistent(self, image_storage):
        ref = f"{image_storage.registry_url}/test-ns/nope:v1"
        assert await image_storage.delete(ref) is False

    async def test_push_idempotent(self, image_storage, local_image):
        ref = f"{image_storage.registry_url}/test-ns/idempotent:v1"
        await image_storage.push_from_local(local_image, ref)
        await image_storage.push_from_local(local_image, ref)
        assert await image_storage.exists(ref) is True
        await image_storage.delete(ref)
