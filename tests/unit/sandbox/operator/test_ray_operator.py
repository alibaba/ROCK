from unittest.mock import MagicMock

import pytest

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import GET_STATUS_SWITCH
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


@pytest.mark.need_ray
def test_use_rocklet_returns_false_when_nacos_provider_is_none(ray_service):
    """When _nacos_provider is None, use_rocklet should return False"""
    operator = RayOperator(ray_service=ray_service)
    operator.set_nacos_provider(None)
    assert operator.use_rocklet() is False


@pytest.mark.need_ray
def test_use_rocklet_returns_false_when_switch_is_off(ray_service):
    """When switch status is False, use_rocklet should return False"""
    operator = RayOperator(ray_service=ray_service)
    mock_nacos_provider = MagicMock()
    mock_nacos_provider.get_switch_status.return_value = False
    operator.set_nacos_provider(mock_nacos_provider)
    assert operator.use_rocklet() is False
    mock_nacos_provider.get_switch_status.assert_called_once_with(GET_STATUS_SWITCH)


@pytest.mark.need_ray
def test_use_rocklet_returns_true_when_switch_is_on(ray_service):
    """When switch status is True, use_rocklet should return True"""
    operator = RayOperator(ray_service=ray_service)
    mock_nacos_provider = MagicMock()
    mock_nacos_provider.get_switch_status.return_value = True
    operator.set_nacos_provider(mock_nacos_provider)
    assert operator.use_rocklet() is True
    mock_nacos_provider.get_switch_status.assert_called_once_with(GET_STATUS_SWITCH)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_ray_operator(ray_service):
    operator = RayOperator(ray_service=ray_service)
    operator.set_nacos_provider(None)
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
