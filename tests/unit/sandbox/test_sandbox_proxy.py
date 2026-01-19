import pytest

from rock.actions.sandbox.response import State
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from tests.unit.conftest import check_sandbox_status_until_alive


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_batch_get_sandbox_status(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    sandbox_ids = []
    sandbox_count = 3
    for _ in range(sandbox_count):
        response = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=1, memory="2g"))
        sandbox_ids.append(response.sandbox_id)
        await check_sandbox_status_until_alive(sandbox_manager, response.sandbox_id)
    # batch get status
    batch_response = await sandbox_proxy_service.batch_get_sandbox_status_from_redis(sandbox_ids)

    assert len(batch_response) == sandbox_count
    response_sandbox_ids = [status.sandbox_id for status in batch_response]
    for sandbox_id in sandbox_ids:
        assert sandbox_id in response_sandbox_ids

    for status in batch_response:
        assert status.sandbox_id in sandbox_ids
        assert status.is_alive is True
        assert status.state == State.RUNNING

    invalid_ids = sandbox_ids + ["invalid_sandbox_id_1", "invalid_sandbox_id_2"]
    batch_response_with_invalid = await sandbox_proxy_service.batch_get_sandbox_status_from_redis(invalid_ids)
    assert len(batch_response_with_invalid) == len(sandbox_ids)
    for sandbox_id in sandbox_ids:
        await sandbox_manager.stop(sandbox_id)
