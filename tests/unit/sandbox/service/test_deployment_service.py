import pytest
from rock.actions.sandbox.response import SystemResourceMetrics


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_actor_not_exist_raises_value_error(ray_deployment_service):
    sandbox_id = "unknown"
    with pytest.raises(Exception) as exc_info:
        await ray_deployment_service._ray_service.async_ray_get_actor(sandbox_id)
    assert exc_info.type == ValueError


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_collect_system_resource_metrics(ray_deployment_service):
    metrics: SystemResourceMetrics = await ray_deployment_service.collect_system_resource_metrics()
    assert metrics.total_cpu > 0
    assert metrics.total_memory > 0
    assert metrics.available_cpu >= 0
    assert metrics.available_memory >= 0
    assert metrics.available_cpu <= metrics.total_cpu
    assert metrics.available_memory <= metrics.total_memory
    # 测试利用率计算
    cpu_utilization = metrics.get_cpu_utilization()
    assert 0.0 <= cpu_utilization <= 1.0
    memory_utilization = metrics.get_memory_utilization()
    assert 0.0 <= memory_utilization <= 1.0