import time
from collections.abc import Callable
from uuid import uuid4

from redis.exceptions import WatchError

from rock.admin.core.redis_key import opensandbox_sessions_key
from rock.utils.providers import RedisProvider

_RESERVATION_PREFIX = "__rock_session_creating__:"
_DEFAULT_RESERVATION_TTL_SECONDS = 120
_MAX_TRANSACTION_ATTEMPTS = 5


class OpenSandboxSessionRegistry:
    """Persist ROCK session-name mappings across Admin workers."""

    def __init__(
        self,
        redis_provider: RedisProvider,
        reservation_ttl_seconds: float = _DEFAULT_RESERVATION_TTL_SECONDS,
    ):
        self._redis = redis_provider
        self._reservation_ttl_seconds = reservation_ttl_seconds

    def _client(self):
        return self._redis._ensure_client()

    @staticmethod
    def _is_expired_reservation(value: str | None, now: float) -> bool:
        if value is None:
            return True
        if not value.startswith(_RESERVATION_PREFIX):
            return False
        try:
            deadline = float(value[len(_RESERVATION_PREFIX) :].split(":", 1)[0])
        except (TypeError, ValueError):
            return False
        return deadline <= now

    async def _update_if(
        self,
        sandbox_id: str,
        session_name: str,
        condition: Callable[[str | None], bool],
        value: str | None,
    ) -> bool:
        key = opensandbox_sessions_key(sandbox_id)
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                async with self._client().pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    current = await pipe.hget(key, session_name)
                    if not condition(current):
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    if value is None:
                        pipe.hdel(key, session_name)
                    else:
                        pipe.hset(key, session_name, value)
                    await pipe.execute()
                    return True
            except WatchError:
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise

        raise AssertionError("unreachable")

    async def reserve(self, sandbox_id: str, session_name: str) -> str | None:
        now = time.time()
        reservation = f"{_RESERVATION_PREFIX}{now + self._reservation_ttl_seconds}:{uuid4().hex}"
        updated = await self._update_if(
            sandbox_id,
            session_name,
            lambda current: self._is_expired_reservation(current, now),
            reservation,
        )
        return reservation if updated else None

    async def commit(self, sandbox_id: str, session_name: str, reservation: str, session_id: str) -> bool:
        return await self._update_if(
            sandbox_id,
            session_name,
            lambda current: current == reservation,
            session_id,
        )

    async def rollback(self, sandbox_id: str, session_name: str, reservation: str) -> bool:
        return await self._update_if(
            sandbox_id,
            session_name,
            lambda current: current == reservation,
            None,
        )

    async def remove(self, sandbox_id: str, session_name: str, session_id: str) -> bool:
        return await self._update_if(
            sandbox_id,
            session_name,
            lambda current: current == session_id,
            None,
        )

    async def get(self, sandbox_id: str, session_name: str) -> str | None:
        session_id = await self._client().hget(opensandbox_sessions_key(sandbox_id), session_name)
        return None if session_id is None or session_id.startswith(_RESERVATION_PREFIX) else session_id

    async def clear(self, sandbox_id: str) -> None:
        await self._client().delete(opensandbox_sessions_key(sandbox_id))
