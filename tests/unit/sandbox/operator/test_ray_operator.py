import pytest

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


@pytest.mark.asyncio
async def test_ray_operator(ray_service):
    operator = RayOperator(ray_service=ray_service)
    start_response: SandboxInfo = await operator.submit(DockerDeploymentConfig(container_name="test"))
    assert start_response.get("sandbox_id") == "test"
    assert start_response.get("host_name") is not None
    assert start_response.get("host_ip") is not None

    status_response: SandboxInfo = await operator.get_status("test")
    assert status_response.get("sandbox_id") == "test"
    assert start_response.get("host_name") is not None
    assert start_response.get("host_ip") is not None

    stop_response: bool = await operator.stop("test")
    assert stop_response
