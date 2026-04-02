"""SandboxMetaStore - coordinator for Redis (hot path) + DB (query path) dual-write.

Redis remains the source of truth for live sandbox state.
The database is an async replica used for indexed queries (list_by, etc.).
All DB operations are awaited for consistency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from rock.actions.sandbox._generated_types import SandboxInfoField
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.core.sandbox_table import SandboxTable
from rock.logger import init_logger
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)

# States that indicate an active sandbox (not yet stopped/archived).
_ACTIVE_STATES: list[str] = [State.RUNNING, State.PENDING]


class SandboxMetaStore:
    """Coordinates sandbox metadata across Redis (hot path) and DB (query path).

    Parameters
    ----------
    redis_provider:
        ``RedisProvider`` instance.  When *None*, all Redis operations are no-ops.
    sandbox_table:
        ``SandboxTable`` instance.  When *None*, all DB operations are no-ops.
    """

    def __init__(
        self,
        redis_provider: RedisProvider | None = None,
        sandbox_table: SandboxTable | None = None,
    ) -> None:
        self._redis: RedisProvider | None = redis_provider
        self._db: SandboxTable | None = sandbox_table

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        sandbox_id: str,
        sandbox_info: SandboxInfo,
        timeout_info: dict[str, str] | None = None,
    ) -> None:
        """Write sandbox info to the Redis alive key and await DB insert.

        Parameters
        ----------
        timeout_info:
            If provided, also write the timeout key (``auto_clear_time`` / ``expire_time``).
        """
        if self._redis:
            await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
            if timeout_info is not None:
                await self._redis.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)

        if self._db:
            await self._safe_db_call(self._db.create, sandbox_id, sandbox_info)

    async def update(self, sandbox_id: str, sandbox_info: SandboxInfo) -> None:
        """Merge *sandbox_info* into the existing Redis alive key and await DB update."""
        if self._redis:
            current = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
            merged: dict[str, Any] = {**(current[0] if current else {}), **sandbox_info}
            await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", merged)

        if self._db:
            await self._safe_db_call(self._db.update, sandbox_id, sandbox_info)

    async def delete(self, sandbox_id: str) -> None:
        """Delete Redis alive + timeout keys and await DB delete."""
        if self._redis:
            await self._redis.json_delete(alive_sandbox_key(sandbox_id))
            await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

        if self._db:
            await self._safe_db_call(self._db.delete, sandbox_id)

    async def archive(self, sandbox_id: str, final_info: SandboxInfo) -> None:
        """Persist final state to DB, then remove sandbox from Redis.

        Unlike ``delete``, the DB record is preserved and updated with
        ``final_info`` (e.g. ``stop_time``, ``state``).  Use this when a
        sandbox has finished its lifecycle and the final state should be
        queryable from the DB.

        The DB write is awaited before the Redis keys are deleted so that
        the final state is always durably stored before the alive key
        disappears.  If the DB write fails the exception is swallowed and
        logged, but Redis cleanup still proceeds.
        """
        if self._db:
            await self._safe_db_call(self._db.update, sandbox_id, final_info)

        if self._redis:
            await self._redis.json_delete(alive_sandbox_key(sandbox_id))
            await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        """Read sandbox info from the Redis alive key."""
        if not self._redis:
            return None

        result = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        if result and len(result) > 0:
            return result[0]
        return None

    async def exists(self, sandbox_id: str) -> bool:
        """Return ``True`` when the Redis alive key exists for ``sandbox_id``."""
        return await self.get(sandbox_id) is not None

    async def get_timeout(self, sandbox_id: str) -> dict[str, str] | None:
        """Read timeout info from the Redis timeout key."""
        if not self._redis:
            return None

        timeout_info = await self._redis.json_get(timeout_sandbox_key(sandbox_id), "$")
        if timeout_info and len(timeout_info) > 0:
            return timeout_info[0]
        return None

    async def update_timeout(self, sandbox_id: str, timeout_info: dict[str, str]) -> None:
        """Overwrite the Redis timeout key with *timeout_info*."""
        if self._redis:
            await self._redis.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)

    async def iter_alive_sandbox_ids(self) -> AsyncIterator[str]:
        """Yield active sandbox IDs.

        Use DB when configured; otherwise fall back to Redis alive keys.
        """
        if self._db:
            for sandbox_info in await self._db.list_by_in("state", _ACTIVE_STATES):
                sandbox_id = sandbox_info.get("sandbox_id")
                if sandbox_id:
                    yield sandbox_id
            return

        if self._redis:
            async for key in self._redis.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=1000):  # type: ignore[attr-defined]
                if not isinstance(key, str):
                    continue
                sandbox_id = key.removeprefix(ALIVE_PREFIX)
                if not sandbox_id:
                    continue
                sandbox_info = await self.get(sandbox_id)
                if sandbox_info and sandbox_info.get("state") in _ACTIVE_STATES:
                    yield sandbox_id

    async def batch_get(self, sandbox_ids: list[str]) -> list[SandboxInfo | None]:
        """Fetch sandbox info for multiple IDs.

        Tries Redis first (alive key).  For any ID that is absent in Redis —
        because the sandbox has been archived and its alive key deleted — falls
        back to the DB so callers receive the final persisted state.
        """
        if not sandbox_ids:
            return []

        results: list[SandboxInfo | None] = [None] * len(sandbox_ids)

        if self._redis:
            alive_keys = [alive_sandbox_key(sid) for sid in sandbox_ids]
            redis_results = await self._redis.json_mget(alive_keys, "$")
            for i, info in enumerate(redis_results):
                results[i] = info if info else None

        if self._db:
            miss_indices = [i for i, r in enumerate(results) if r is None]
            if miss_indices:
                miss_ids = [sandbox_ids[i] for i in miss_indices]
                db_records = await self._db.list_by_in("sandbox_id", miss_ids)
                db_map = {r["sandbox_id"]: r for r in db_records if r.get("sandbox_id")}
                for i in miss_indices:
                    results[i] = db_map.get(sandbox_ids[i])

        return results

    async def list_by(self, field: SandboxInfoField, value: str | int | float | bool) -> list[SandboxInfo]:
        """Query sandboxes by *field* == *value*.

        Prefer the DB when configured; otherwise fall back to filtering Redis alive records.
        """
        if self._db:
            try:
                return await self._db.list_by(field, value)
            except ValueError:
                # Some fields are intentionally not in DB allowlist.
                # In that case, gracefully fall back to Redis filtering.
                if not self._redis:
                    raise
                logger.info("list_by fallback to Redis for non-allowlisted DB field: %s", field)
        if not self._redis:
            return []

        results: list[SandboxInfo] = []
        async for key in self._redis.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=1000):  # type: ignore[attr-defined]
            if not isinstance(key, str):
                continue
            redis_info = await self._redis.json_get(key, "$")
            if not redis_info:
                continue
            sandbox_info = redis_info[0]
            if sandbox_info.get(field) == value:
                results.append(sandbox_info)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_db_call(fn: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """Shared error handler: swallows and logs DB exceptions so they never propagate."""
        try:
            await fn(*args)
        except Exception:
            logger.warning("DB write failed", exc_info=True)
