"""Tests for RedisProvider against a real Redis Stack container (with RedisJSON)."""

import pytest

from rock.utils.providers.redis_provider import RedisProvider


@pytest.mark.need_docker
class TestRedisProviderWithDocker:
    """Integration tests for RedisProvider using a real Redis Stack container."""

    @pytest.fixture
    async def redis(self, redis_container):
        """Create a RedisProvider connected to the test Redis container."""
        provider = RedisProvider(
            host=redis_container["host"],
            port=redis_container["port"],
            password=redis_container["password"],
        )
        await provider.init_pool()
        yield provider
        await provider.close_pool()

    async def test_fixture_connection(self, redis):
        """Verify the fixture can connect and ping Redis."""
        assert redis.client is not None
        pong = await redis.client.ping()
        assert pong is True

    async def test_json_set_and_get(self, redis):
        key = "test:sandbox:001"
        data = {"sandbox_id": "sb-001", "state": "RUNNING", "user_id": "alice"}

        await redis.json_set(key, "$", data)
        result = await redis.json_get(key, "$")

        assert result is not None
        assert result[0]["sandbox_id"] == "sb-001"
        assert result[0]["state"] == "RUNNING"

    async def test_json_get_subpath(self, redis):
        key = "test:sandbox:002"
        data = {"sandbox_id": "sb-002", "spec": {"cpus": 4, "memory": "8Gi"}}

        await redis.json_set(key, "$", data)
        result = await redis.json_get(key, "$.spec.cpus")

        assert result == 4

    async def test_json_set_with_ttl(self, redis):
        key = "test:sandbox:ttl"
        data = {"sandbox_id": "sb-ttl"}

        await redis.json_set_with_ttl(key, "$", data, ttl_seconds=300)

        ttl = await redis.get_ttl(key)
        assert ttl is not None
        assert ttl > 0
        assert ttl <= 300

    async def test_json_delete(self, redis):
        key = "test:sandbox:del"
        await redis.json_set(key, "$", {"sandbox_id": "sb-del"})

        deleted = await redis.json_delete(key)
        assert deleted >= 1

        result = await redis.json_get(key, "$")
        assert result is None

    async def test_json_mget(self, redis):
        keys = ["test:mget:1", "test:mget:2", "test:mget:3"]
        for i, key in enumerate(keys):
            await redis.json_set(key, "$", {"name": f"user-{i}"})

        results = await redis.json_mget(keys, "$")
        assert len(results) == 3
        assert results[0][0]["name"] == "user-0"
        assert results[2][0]["name"] == "user-2"

    async def test_pattern_exists(self, redis):
        await redis.json_set("test:pattern:hit", "$", {"v": 1})

        assert await redis.pattern_exists("test:pattern:*") is True
        assert await redis.pattern_exists("nonexistent:prefix:*") is False

    async def test_get_nonexistent_key(self, redis):
        result = await redis.json_get("does-not-exist", "$")
        assert result is None
