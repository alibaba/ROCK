import pytest


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_get_actor_not_exist_raises_value_error(ray_deployment_service):
    sandbox_id = "unknown"
    with pytest.raises(Exception) as exc_info:
        await ray_deployment_service.async_ray_get_actor(sandbox_id)
    assert exc_info.type == ValueError