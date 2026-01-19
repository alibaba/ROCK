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
        response = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"))
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


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_list_sandbox(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    # 创建多个 sandbox
    user_info1 = {"user_id": "user1", "experiment_id": "exp1"}
    user_info2 = {"user_id": "user2", "experiment_id": "exp2"}
    response1 = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"), user_info=user_info1)
    response2 = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"), user_info=user_info2)
    sandbox_id1 = response1.sandbox_id
    sandbox_id2 = response2.sandbox_id
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id1)
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id2)
    # 不带过滤条件查询
    result = await sandbox_proxy_service.list_sandboxes({})
    assert len(result.items) >= 2
    sandbox_ids = [s.sandbox_id for s in result.items]
    assert sandbox_id1 in sandbox_ids
    assert sandbox_id2 in sandbox_ids
    # 带过滤条件查询
    result = await sandbox_proxy_service.list_sandboxes(
        {"user_id": user_info1["user_id"], "experiment_id": user_info1["experiment_id"]}
    )
    # 验证只返回匹配的 sandbox
    assert len(result.items) >= 1
    for sandbox_data in result.items:
        assert sandbox_data.user_id == user_info1["user_id"]
        assert sandbox_data.experiment_id == user_info1["experiment_id"]
    sandbox_ids = [s.sandbox_id for s in result.items]
    assert sandbox_id1 in sandbox_ids
    # 验证返回空列表
    result = await sandbox_proxy_service.list_sandboxes(
        {"user_id": user_info1["user_id"], "experiment_id": user_info2["experiment_id"]}
    )
    assert len(result.items) == 0
    await sandbox_manager.stop(sandbox_id1)
    await sandbox_manager.stop(sandbox_id2)
