import pytest

from rock.utils.providers.redis_provider import RedisProvider


@pytest.mark.asyncio
async def test_redis_json_mget(redis_provider: RedisProvider):
    test_keys = ["test:mget:1", "test:mget:2", "test:mget:3"]
    test_data = [
        {"name": "user1", "age": 25, "city": "Beijing"},
        {"name": "user2", "age": 30, "city": "Shanghai"},
        {"name": "user3", "age": 35, "city": "Guangzhou"},
    ]
    for key, data in zip(test_keys, test_data):
        await redis_provider.json_set(key, "$", data)

    results = await redis_provider.json_mget(test_keys, "$")
    assert len(results) == 3
    assert results[0]["name"] == "user1"
    assert results[1]["age"] == 30
    assert results[2]["city"] == "Guangzhou"

    names = await redis_provider.json_mget(test_keys, "$.name")
    assert names == ["user1", "user2", "user3"]

    mixed_keys = ["test:mget:1", "test:mget:nonexistent", "test:mget:3"]
    mixed_results = await redis_provider.json_mget(mixed_keys, "$")
    assert len(mixed_results) == 3
    assert not mixed_results[1]

    for key in test_keys:
        await redis_provider.json_delete(key)

    cleanup_results = await redis_provider.json_mget(test_keys, "$")
    assert all(not result for result in cleanup_results)
