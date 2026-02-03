import pytest

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


@pytest.mark.asyncio
async def test_ray_operator():
    operator = RayOperator()
    start_response: SandboxInfo = await operator.submit(DockerDeploymentConfig())
    assert start_response.get("sandbox_id") == "test"
    assert start_response.get("host_name") == "test"
    assert start_response.get("host_ip") == "test"

    stop_response: bool = await operator.stop("test")
    assert stop_response

    status_response: SandboxInfo = await operator.get_status("test")
    assert status_response.get("sandbox_id") == "test"
    assert status_response.get("host_name") == "test"
    assert status_response.get("host_ip") == "test"