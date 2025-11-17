import pytest

from rock.deployments.remote import RemoteDeployment
from rock.rocklet.exceptions import DeploymentNotStartedError


@pytest.mark.asyncio
async def test_remote_deployment(rocklet_remote_server):
    port = rocklet_remote_server.port
    print(f"Using port {port} for the remote deployment")
    d = RemoteDeployment(port=port)
    with pytest.raises(DeploymentNotStartedError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()
