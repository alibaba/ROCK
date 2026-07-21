import pytest
from redis.exceptions import WatchError

from rock.admin.core.redis_key import opensandbox_sessions_key
from rock.sandbox.operator.opensandbox.session_registry import OpenSandboxSessionRegistry


@pytest.fixture
def registry(redis_provider):
    return OpenSandboxSessionRegistry(redis_provider)


@pytest.mark.asyncio
async def test_reserve_is_atomic_for_duplicate_session_names(registry):
    assert await registry.reserve("sbx-1", "default") is not None
    assert await registry.reserve("sbx-1", "default") is None


@pytest.mark.asyncio
async def test_commit_makes_session_id_available(registry):
    reservation = await registry.reserve("sbx-1", "default")
    assert reservation is not None
    assert await registry.get("sbx-1", "default") is None

    assert await registry.commit("sbx-1", "default", reservation, "os-session-1") is True

    assert await registry.get("sbx-1", "default") == "os-session-1"


@pytest.mark.asyncio
async def test_rollback_releases_owned_reservation(registry):
    reservation = await registry.reserve("sbx-1", "default")
    assert reservation is not None

    assert await registry.rollback("sbx-1", "default", reservation) is True

    assert await registry.reserve("sbx-1", "default") is not None


@pytest.mark.asyncio
async def test_remove_deletes_committed_session(registry):
    reservation = await registry.reserve("sbx-1", "default")
    assert reservation is not None
    assert await registry.commit("sbx-1", "default", reservation, "os-session-1") is True

    assert await registry.remove("sbx-1", "default", "os-session-1") is True

    assert await registry.get("sbx-1", "default") is None


@pytest.mark.asyncio
async def test_stale_session_cannot_remove_replacement_mapping(registry):
    first = await registry.reserve("sbx-1", "default")
    assert first is not None
    assert await registry.commit("sbx-1", "default", first, "old-session") is True
    assert await registry.remove("sbx-1", "default", "old-session") is True

    second = await registry.reserve("sbx-1", "default")
    assert second is not None
    assert await registry.commit("sbx-1", "default", second, "new-session") is True

    assert await registry.remove("sbx-1", "default", "old-session") is False
    assert await registry.get("sbx-1", "default") == "new-session"


@pytest.mark.asyncio
async def test_clear_removes_all_sessions_for_one_sandbox_only(registry):
    for sandbox_id, name, session_id in (
        ("sbx-1", "default", "os-session-1"),
        ("sbx-1", "worker", "os-session-2"),
        ("sbx-2", "default", "os-session-3"),
    ):
        reservation = await registry.reserve(sandbox_id, name)
        assert reservation is not None
        assert await registry.commit(sandbox_id, name, reservation, session_id) is True

    await registry.clear("sbx-1")

    assert await registry.get("sbx-1", "default") is None
    assert await registry.get("sbx-1", "worker") is None
    assert await registry.get("sbx-2", "default") == "os-session-3"


def test_registry_key_is_namespaced_by_sandbox():
    assert opensandbox_sessions_key("sbx-1") == "opensandbox:sessions:sbx-1"


@pytest.mark.asyncio
async def test_expired_reservation_can_be_reclaimed(redis_provider):
    registry = OpenSandboxSessionRegistry(redis_provider, reservation_ttl_seconds=0)
    first = await registry.reserve("sbx-1", "default")
    second = await registry.reserve("sbx-1", "default")

    assert first is not None
    assert second is not None
    assert second != first


@pytest.mark.asyncio
async def test_stale_owner_cannot_commit_or_rollback_new_reservation(redis_provider):
    registry = OpenSandboxSessionRegistry(redis_provider, reservation_ttl_seconds=0)
    first = await registry.reserve("sbx-1", "default")
    second = await registry.reserve("sbx-1", "default")
    assert first is not None and second is not None

    assert await registry.commit("sbx-1", "default", first, "stale-session") is False
    assert await registry.rollback("sbx-1", "default", first) is False
    assert await registry.commit("sbx-1", "default", second, "current-session") is True
    assert await registry.get("sbx-1", "default") == "current-session"


@pytest.mark.asyncio
async def test_update_if_applies_conditional_set_and_delete(registry):
    def is_empty(current):
        return current is None

    def is_session(current):
        return current == "os-session-1"

    assert await registry._update_if("sbx-1", "default", is_empty, "os-session-1") is True
    assert await registry._update_if("sbx-1", "default", is_empty, "replacement") is False
    assert await registry.get("sbx-1", "default") == "os-session-1"

    assert await registry._update_if("sbx-1", "default", is_session, None) is True
    assert await registry.get("sbx-1", "default") is None


@pytest.mark.asyncio
async def test_update_if_stops_after_repeated_watch_conflicts(registry, monkeypatch):
    attempts = 0
    max_attempts = 5

    class ConflictingPipeline:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def watch(self, _key):
            return None

        async def hget(self, _key, _field):
            return None

        def multi(self):
            return None

        def hset(self, _key, _field, _value):
            return None

        async def execute(self):
            nonlocal attempts
            attempts += 1
            if attempts > max_attempts:
                raise AssertionError("transaction retry limit was exceeded")
            raise WatchError

    class ConflictingClient:
        def pipeline(self, *, transaction):
            assert transaction is True
            return ConflictingPipeline()

    monkeypatch.setattr(registry, "_client", lambda: ConflictingClient())

    with pytest.raises(WatchError):
        await registry._update_if("sbx-1", "default", lambda current: current is None, "os-session-1")

    assert attempts == max_attempts
