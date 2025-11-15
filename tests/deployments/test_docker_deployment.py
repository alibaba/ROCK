import pytest

from rock.deployments.local import LocalDeployment
from rock.utils.docker import DockerUtil


@pytest.mark.asyncio
async def test_local_deployment():
    d = LocalDeployment()
    assert not await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()
    assert not await d.is_alive()


async def test_docker():
    docker_util = DockerUtil()
    assert docker_util.is_docker_available()
    assert docker_util.is_image_available("hello-world:latest")
