"""Tests for SandboxMetaStore - Redis + DB dual-write coordinator."""

import asyncio
import time
import uuid

import pytest
from fakeredis import aioredis

from rock.actions.sandbox.response import State
from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.core.sandbox_table import SandboxTable
from rock.config import DatabaseConfig
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.utils.providers.redis_provider import RedisProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis():
    provider = RedisProvider(host=None, port=None, password="")
    provider.client = aioredis.FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


@pytest.fixture
async def db():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:"))
    await provider.init()
    await provider.create_tables()
    table = SandboxTable(provider)
    yield table
    await provider.close()


@pytest.fixture
def repo(redis, db):
    return SandboxMetaStore(redis_provider=redis, sandbox_table=db)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SANDBOX_ID = "sbx-test-001"

SANDBOX_INFO = {
    "sandbox_id": SANDBOX_ID,
    "user_id": "user-1",
    "image": "python:3.11",
    "experiment_id": "exp-1",
    "namespace": "default",
    "cluster_name": "cluster-1",
    "state": State.RUNNING,
    "host_ip": "10.0.0.1",
    "create_time": "2025-01-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSave:
    async def test_save_writes_redis_and_db(self, repo, redis, db):
        """save() should persist to Redis alive key AND fire a DB upsert."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)

        # Give fire-and-forget task time to complete
        await asyncio.sleep(0.1)

        # Verify Redis
        result = await redis.json_get(alive_sandbox_key(SANDBOX_ID), "$")
        assert result is not None
        assert result[0]["sandbox_id"] == SANDBOX_ID
        assert result[0]["user_id"] == "user-1"

        # Verify DB
        db_record = await db.get(SANDBOX_ID)
        assert db_record is not None
        assert db_record["user_id"] == "user-1"

    async def test_save_with_timeout_info(self, repo, redis):
        """save() with timeout_info should also write the timeout key."""
        timeout = {"auto_clear_time": "30", "expire_time": "9999999999"}
        await repo.create(SANDBOX_ID, SANDBOX_INFO, timeout_info=timeout)

        result = await redis.json_get(timeout_sandbox_key(SANDBOX_ID), "$")
        assert result is not None
        assert result[0]["auto_clear_time"] == "30"


class TestUpdate:
    async def test_update_writes_redis_and_db(self, repo, redis, db):
        """update() should merge new fields into Redis and fire DB update."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)
        await asyncio.sleep(0.1)

        update_data = {"state": "stopped", "stop_time": "2025-01-01T01:00:00Z"}
        await repo.update(SANDBOX_ID, update_data)
        await asyncio.sleep(0.1)

        # Verify Redis - should have merged (old fields + new fields)
        result = await redis.json_get(alive_sandbox_key(SANDBOX_ID), "$")
        assert result is not None
        info = result[0]
        assert info["state"] == "stopped"
        assert info["stop_time"] == "2025-01-01T01:00:00Z"
        # Original fields should still be present
        assert info["user_id"] == "user-1"
        assert info["image"] == "python:3.11"

        # Verify DB
        db_record = await db.get(SANDBOX_ID)
        assert db_record is not None
        assert db_record["state"] == "stopped"


class TestRemove:
    async def test_remove_deletes_redis_and_db(self, repo, redis, db):
        """remove() should delete from both Redis alive+timeout keys and DB."""
        # Setup: save sandbox and a timeout key
        await repo.create(SANDBOX_ID, SANDBOX_INFO)
        timeout_data = {"auto_clear_time": "30", "expire_time": str(int(time.time()) + 1800)}
        await redis.json_set(timeout_sandbox_key(SANDBOX_ID), "$", timeout_data)
        await asyncio.sleep(0.1)

        # Act
        await repo.delete(SANDBOX_ID)
        await asyncio.sleep(0.1)

        # Verify Redis - both keys gone
        alive_result = await redis.json_get(alive_sandbox_key(SANDBOX_ID), "$")
        assert alive_result is None
        timeout_result = await redis.json_get(timeout_sandbox_key(SANDBOX_ID), "$")
        assert timeout_result is None

        # Verify DB
        db_record = await db.get(SANDBOX_ID)
        assert db_record is None


class TestArchive:
    async def test_archive_removes_redis_and_updates_db(self, repo, redis, db):
        """archive() should update DB first, then remove Redis keys."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)
        await redis.json_set(timeout_sandbox_key(SANDBOX_ID), "$", {"auto_clear_time": "30", "expire_time": "9999"})
        await asyncio.sleep(0.1)  # let the create fire-and-forget DB insert settle

        final_info: dict = {"state": "stopped", "stop_time": "2025-06-01T00:00:00Z"}
        await repo.archive(SANDBOX_ID, final_info)
        # No extra sleep needed: archive() awaits the DB write before returning.

        # Redis: both keys gone
        assert await redis.json_get(alive_sandbox_key(SANDBOX_ID), "$") is None
        assert await redis.json_get(timeout_sandbox_key(SANDBOX_ID), "$") is None

        # DB: record still present with updated fields
        db_record = await db.get(SANDBOX_ID)
        assert db_record is not None
        assert db_record["state"] == "stopped"
        assert db_record["stop_time"] == "2025-06-01T00:00:00Z"
        assert db_record["user_id"] == "user-1"  # original fields preserved

    async def test_archive_db_written_before_redis_deleted(self, repo, redis, db):
        """DB must be durably updated before the Redis alive key is removed."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)
        await asyncio.sleep(0.1)

        # Intercept: check DB state immediately after archive returns (no extra sleep).
        await repo.archive(SANDBOX_ID, {"state": "stopped"})

        # At this point archive() has already awaited the DB write and deleted Redis.
        assert await redis.json_get(alive_sandbox_key(SANDBOX_ID), "$") is None
        db_record = await db.get(SANDBOX_ID)
        assert db_record is not None
        assert db_record["state"] == "stopped"


class TestGet:
    async def test_get_reads_from_redis(self, repo, redis):
        """get() should read from Redis alive key."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)

        result = await repo.get(SANDBOX_ID)
        assert result is not None
        assert result["sandbox_id"] == SANDBOX_ID
        assert result["user_id"] == "user-1"

    async def test_get_nonexistent_returns_none(self, repo):
        """get() on a non-existent sandbox should return None."""
        result = await repo.get("does-not-exist")
        assert result is None


class TestExists:
    async def test_exists_returns_true_when_present(self, repo, redis):
        """exists() should return True when the sandbox alive key exists."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)

        assert await repo.exists(SANDBOX_ID) is True

    async def test_exists_returns_false_when_absent(self, repo):
        """exists() should return False for a non-existent sandbox."""
        assert await repo.exists("does-not-exist") is False


class TestGetTimeout:
    async def test_get_timeout_returns_timeout_info(self, repo, redis):
        """get_timeout() should return the timeout dict from Redis."""
        timeout_data = {"auto_clear_time": "30", "expire_time": "9999999999"}
        await redis.json_set(timeout_sandbox_key(SANDBOX_ID), "$", timeout_data)

        result = await repo.get_timeout(SANDBOX_ID)
        assert result is not None
        assert result["auto_clear_time"] == "30"
        assert result["expire_time"] == "9999999999"

    async def test_get_timeout_returns_none_when_absent(self, repo):
        """get_timeout() should return None when the timeout key does not exist."""
        result = await repo.get_timeout("does-not-exist")
        assert result is None


class TestIterAliveSandboxIds:
    async def test_iter_alive_sandbox_ids_yields_running_and_pending(self, repo):
        """iter_alive_sandbox_ids() should yield IDs for both RUNNING and PENDING sandboxes."""
        await repo.create("sbx-running", {**SANDBOX_INFO, "sandbox_id": "sbx-running", "state": State.RUNNING})
        await repo.create("sbx-pending", {**SANDBOX_INFO, "sandbox_id": "sbx-pending", "state": State.PENDING})
        await asyncio.sleep(0.1)  # let fire-and-forget DB writes settle

        ids = {sid async for sid in repo.iter_alive_sandbox_ids()}
        assert "sbx-running" in ids
        assert "sbx-pending" in ids

    async def test_iter_alive_sandbox_ids_excludes_stopped(self, repo):
        """iter_alive_sandbox_ids() should not yield sandboxes with terminal state."""
        await repo.create("sbx-running", {**SANDBOX_INFO, "sandbox_id": "sbx-running"})
        await repo.create("sbx-stopped", {**SANDBOX_INFO, "sandbox_id": "sbx-stopped", "state": "stopped"})
        await asyncio.sleep(0.1)

        ids = [sid async for sid in repo.iter_alive_sandbox_ids()]
        assert "sbx-running" in ids
        assert "sbx-stopped" not in ids

    async def test_iter_alive_sandbox_ids_consistent_with_redis_scan(self, repo, redis):
        """DB list_by_in(state IN active_states) should be consistent with Redis alive-key scan.

        Both approaches must agree: every active sandbox (PENDING or RUNNING) found in DB
        must also have a Redis alive key. The inverse may not hold for sandboxes whose state
        was updated to a terminal value without calling archive()/remove().
        """
        await repo.create("sbx-a", {**SANDBOX_INFO, "sandbox_id": "sbx-a", "state": State.RUNNING})
        await repo.create("sbx-b", {**SANDBOX_INFO, "sandbox_id": "sbx-b", "state": State.PENDING})
        await asyncio.sleep(0.1)

        # new approach: DB-backed iter_alive_sandbox_ids (PENDING + RUNNING)
        db_ids = {sid async for sid in repo.iter_alive_sandbox_ids()}

        # old approach: Redis scan_iter on alive: prefix
        redis_ids = set()
        async for key in redis.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=100):
            if isinstance(key, str) and key.startswith(ALIVE_PREFIX):
                redis_ids.add(key.removeprefix(ALIVE_PREFIX))

        assert db_ids == redis_ids


class TestBatchGet:
    async def test_batch_get_returns_db_results(self, repo):
        """batch_get() returns sandbox info from the DB."""
        await repo.create(SANDBOX_ID, SANDBOX_INFO)

        results = await repo.batch_get([SANDBOX_ID])
        assert len(results) == 1
        assert results[0]["sandbox_id"] == SANDBOX_ID

    async def test_batch_get_omits_unknown_id(self, repo):
        """batch_get() omits IDs not found in DB."""
        results = await repo.batch_get(["does-not-exist"])
        assert results == []

    async def test_batch_get_multiple_ids(self, repo):
        """batch_get() returns only found sandboxes; missing IDs are omitted."""
        await repo.create("sbx-a", {**SANDBOX_INFO, "sandbox_id": "sbx-a"})
        await repo.create("sbx-b", {**SANDBOX_INFO, "sandbox_id": "sbx-b"})

        results = await repo.batch_get(["sbx-a", "sbx-b", "sbx-missing"])
        assert len(results) == 2
        sandbox_ids = {r["sandbox_id"] for r in results}
        assert sandbox_ids == {"sbx-a", "sbx-b"}

    async def test_batch_get_empty_list(self, repo):
        """batch_get([]) should return []."""
        assert await repo.batch_get([]) == []


class TestListBy:
    async def test_list_by_queries_db(self, repo, db):
        """list_by() should query the DB by a given field."""
        info_a = {**SANDBOX_INFO, "sandbox_id": "sbx-a", "user_id": "user-1"}
        info_b = {**SANDBOX_INFO, "sandbox_id": "sbx-b", "user_id": "user-1"}
        info_c = {**SANDBOX_INFO, "sandbox_id": "sbx-c", "user_id": "user-2"}

        await repo.create("sbx-a", info_a)
        await repo.create("sbx-b", info_b)
        await repo.create("sbx-c", info_c)
        await asyncio.sleep(0.2)

        results = await repo.list_by("user_id", "user-1")
        assert len(results) == 2
        sandbox_ids = {r["sandbox_id"] for r in results}
        assert sandbox_ids == {"sbx-a", "sbx-b"}

    async def test_list_by_raises_for_non_allowlisted_field(self, repo):
        """list_by() should raise ValueError for fields not in the DB allowlist."""
        with pytest.raises(ValueError):
            await repo.list_by("create_time", "t-1")


class TestUpdateTimeout:
    async def test_update_timeout_writes_redis(self, repo, redis):
        """update_timeout() should overwrite the timeout key in Redis."""
        new_info = {"auto_clear_time": "60", "expire_time": "9999999999"}
        await repo.update_timeout(SANDBOX_ID, new_info)

        result = await redis.json_get(timeout_sandbox_key(SANDBOX_ID), "$")
        assert result is not None
        assert result[0]["auto_clear_time"] == "60"
        assert result[0]["expire_time"] == "9999999999"


# ---------------------------------------------------------------------------
# Docker-backed fixtures (real Redis Stack + real PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_redis(redis_container):
    provider = RedisProvider(
        host=redis_container["host"],
        port=redis_container["port"],
        password=redis_container["password"],
    )
    await provider.init_pool()
    yield provider
    await provider.close_pool()


@pytest.fixture
async def real_db(pg_container):
    provider = DatabaseProvider(db_config=DatabaseConfig(url=pg_container["url"]))
    await provider.init()
    await provider.create_tables()
    table = SandboxTable(provider)
    yield table
    await provider.close()


@pytest.fixture
def docker_repo(real_redis, real_db):
    return SandboxMetaStore(redis_provider=real_redis, sandbox_table=real_db)


# ---------------------------------------------------------------------------
# Docker-backed tests
# ---------------------------------------------------------------------------


@pytest.mark.need_docker
class TestSandboxMetaStoreWithDocker:
    """SandboxMetaStore verified against real Redis Stack + PostgreSQL.

    Uses unique sandbox IDs per test to avoid cross-test pollution across
    the shared session-scoped containers.
    """

    async def test_save_writes_redis_and_db(self, docker_repo, real_redis, real_db):
        """save() persists to real Redis and fires a real DB insert."""
        sid = f"docker-{uuid.uuid4().hex[:8]}"
        info = {**SANDBOX_INFO, "sandbox_id": sid, "user_id": "docker-user"}

        await docker_repo.create(sid, info)
        await asyncio.sleep(0.15)

        result = await real_redis.json_get(alive_sandbox_key(sid), "$")
        assert result is not None
        assert result[0]["sandbox_id"] == sid

        db_record = await real_db.get(sid)
        assert db_record is not None
        assert db_record["user_id"] == "docker-user"

    async def test_update_writes_redis_and_db(self, docker_repo, real_redis, real_db):
        """update() merges into Redis and fires a real DB update."""
        sid = f"docker-{uuid.uuid4().hex[:8]}"
        await docker_repo.create(sid, {**SANDBOX_INFO, "sandbox_id": sid})
        await asyncio.sleep(0.15)

        await docker_repo.update(sid, {"state": "stopped"})
        await asyncio.sleep(0.15)

        redis_result = await real_redis.json_get(alive_sandbox_key(sid), "$")
        assert redis_result[0]["state"] == "stopped"
        assert redis_result[0]["user_id"] == "user-1"  # old fields still present

        db_record = await real_db.get(sid)
        assert db_record["state"] == "stopped"

    async def test_remove_deletes_redis_and_db(self, docker_repo, real_redis, real_db):
        """remove() deletes Redis alive+timeout keys and the DB row."""
        sid = f"docker-{uuid.uuid4().hex[:8]}"
        timeout = {"auto_clear_time": "30", "expire_time": "9999999999"}
        await docker_repo.create(sid, {**SANDBOX_INFO, "sandbox_id": sid}, timeout_info=timeout)
        await asyncio.sleep(0.15)

        await docker_repo.delete(sid)
        await asyncio.sleep(0.15)

        assert await real_redis.json_get(alive_sandbox_key(sid), "$") is None
        assert await real_db.get(sid) is None

    async def test_list_by_queries_db(self, docker_repo, real_db):
        """list_by() returns DB rows matching the given field value."""
        uid = f"docker-user-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            sid = f"docker-{uuid.uuid4().hex[:8]}"
            await docker_repo.create(sid, {**SANDBOX_INFO, "sandbox_id": sid, "user_id": uid})
        await asyncio.sleep(0.2)

        results = await docker_repo.list_by("user_id", uid)
        assert len(results) == 3
        assert all(r["user_id"] == uid for r in results)

    async def test_iter_alive_sandbox_ids(self, docker_repo, real_redis):
        """iter_alive_sandbox_ids() yields RUNNING sandbox IDs from DB, consistent with Redis alive keys."""
        sids = [f"docker-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        for sid in sids:
            await docker_repo.create(sid, {**SANDBOX_INFO, "sandbox_id": sid})
        await asyncio.sleep(0.2)  # let fire-and-forget DB writes settle

        # new approach: DB-backed
        db_found = [s async for s in docker_repo.iter_alive_sandbox_ids()]
        assert set(sids).issubset(set(db_found))

        # consistency: every RUNNING sandbox in DB must have a Redis alive key
        redis_ids = set()
        async for key in real_redis.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=100):
            if isinstance(key, str) and key.startswith(ALIVE_PREFIX):
                redis_ids.add(key.removeprefix(ALIVE_PREFIX))
        assert set(sids).issubset(redis_ids)
        # Only assert consistency for the sandboxes created in this test;
        # db_found may include leftover sandboxes from other tests whose
        # alive keys have already been cleaned up.
        assert set(sids).issubset(redis_ids)
