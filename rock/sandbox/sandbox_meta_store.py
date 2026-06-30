"""SandboxMetaStore - coordinator for Redis (hot path) + DB (query path) dual-write.

Redis remains the source of truth for live sandbox state.
The database is an async replica used for indexed queries (list_by, etc.).
All DB operations are awaited for consistency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo, pick_sandbox_info_fields
from rock.admin.core.redis_key import alive_sandbox_key, timeout_sandbox_key
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.metrics.decorator import monitor_metastore_operation
from rock.admin.metrics.monitor import MetricsMonitor
from rock.config import RockConfig

if TYPE_CHECKING:
    from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)

# States that indicate an active sandbox (not yet stopped/archived).
_ACTIVE_STATES: list[str] = [State.RUNNING, State.PENDING]


class SandboxMetaStore:
    """Coordinates sandbox metadata across Redis (hot path) and DB (query path).

    Both providers are required. Use FakeRedis / SQLite-memory for local/test environments.
    """

    def __init__(
        self,
        redis_provider: RedisProvider,
        sandbox_table: SandboxTable,
        rock_config: RockConfig | None = None,
    ) -> None:
        self._redis: RedisProvider = redis_provider
        self._db: SandboxTable = sandbox_table
        self.metrics_monitor = MetricsMonitor.create(
            export_interval_millis=20_000,
            metrics_endpoint=rock_config.runtime.metrics_endpoint if rock_config else "",
            user_defined_tags=rock_config.runtime.user_defined_tags if rock_config else {},
            metric_prefix="meta_store",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @monitor_metastore_operation
    async def create(
        self,
        sandbox_id: str,
        sandbox_info: SandboxInfo,
        timeout_info: dict[str, str] | None = None,
        deployment_config: DockerDeploymentConfig | None = None,
    ) -> None:
        """Write sandbox info to the Redis alive key and await DB insert.

        Parameters
        ----------
        timeout_info:
            If provided, also write the timeout key (``auto_clear_time`` / ``expire_time``).
        deployment_config:
            ``DockerDeploymentConfig`` snapshot written once to the ``spec`` DB column.
            Redis does not store this.

        The Redis payload is filtered to keys declared in ``SandboxInfo`` so any
        DB-only fields the caller may carry (e.g. ``spec`` / ``status`` from a
        prior DB-fallback read) cannot leak into the alive key.
        """
        import time

        redis_payload = pick_sandbox_info_fields(sandbox_info)
        t0 = time.perf_counter()
        await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", redis_payload)
        if timeout_info is not None:
            await self._redis.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)
        logger.info(
            f"[startup_timing] [{sandbox_id}] Meta store create Redis " f"took {time.perf_counter() - t0:.3f} s"
        )
        self._redis.log_pool_detailed_status()

        t0 = time.perf_counter()
        await self._db.create(sandbox_id, sandbox_info, deployment_config)
        logger.info(f"[startup_timing] [{sandbox_id}] Meta store create DB " f"took {time.perf_counter() - t0:.3f} s")

    @monitor_metastore_operation
    async def update(self, sandbox_id: str, sandbox_info: SandboxInfo) -> None:
        """Merge *sandbox_info* into the existing Redis alive key and await DB update.

        The Redis-side merge uses only keys declared in ``SandboxInfo``; DB-only
        fields like ``spec`` / ``status`` are dropped so they don't pollute the
        alive key. The DB write keeps the full dict — the DB layer has its own
        column-based filtering.
        """
        redis_payload = pick_sandbox_info_fields(sandbox_info)
        current = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        merged: dict[str, Any] = {**(current[0] if current else {}), **redis_payload}
        await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", merged)

        await self._db.update(sandbox_id, sandbox_info)

    @monitor_metastore_operation
    async def delete(self, sandbox_id: str) -> None:
        """Delete Redis alive + timeout keys and await DB delete."""
        await self._redis.json_delete(alive_sandbox_key(sandbox_id))
        await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

        await self._db.delete(sandbox_id)

    @monitor_metastore_operation
    async def archive(self, sandbox_id: str, final_info: SandboxInfo | None = None) -> None:
        """Snapshot Redis state to DB, then evict Redis keys.

        Reads the current alive-key from Redis, merges *final_info* on top
        (e.g. ``stop_time``, ``state``), persists the result to the DB, and
        then deletes the Redis alive + timeout keys.

        The DB write is awaited before the Redis keys are deleted so that
        the snapshot is always durably stored before the cache disappears.
        """
        current = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        merged: dict[str, Any] = {**(current[0] if current else {}), **(final_info or {})}
        if merged:
            await self._db.update(sandbox_id, merged)

        await self._redis.json_delete(alive_sandbox_key(sandbox_id))
        await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

    @monitor_metastore_operation
    async def get(self, sandbox_id: str, check_db: bool = False) -> SandboxInfo | None:
        """Read sandbox info from the Redis alive key."""
        result = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        if result and len(result) > 0:
            return result[0]
        if check_db:
            result = await self._db.get(sandbox_id)
            if result:
                return result
        return None

    async def exists(self, sandbox_id: str) -> bool:
        """Return ``True`` when the Redis alive key exists for ``sandbox_id``."""
        # Use EXISTS command instead of JSON.GET for better performance
        key = alive_sandbox_key(sandbox_id)
        return await self._redis.exists(key)

    @monitor_metastore_operation
    async def get_timeout(self, sandbox_id: str) -> dict[str, str] | None:
        """Read timeout info from the Redis timeout key."""
        timeout_info = await self._redis.json_get(timeout_sandbox_key(sandbox_id), "$")
        if timeout_info and len(timeout_info) > 0:
            return timeout_info[0]
        return None

    @monitor_metastore_operation
    async def update_timeout(self, sandbox_id: str, timeout_info: dict[str, str]) -> None:
        """Overwrite the Redis timeout key with *timeout_info*."""
        await self._redis.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)

    async def iter_alive_sandbox_ids(self) -> AsyncIterator[str]:
        """Yield active sandbox IDs from the DB."""
        for sandbox_info in await self._db.list_by_in("state", _ACTIVE_STATES):
            sandbox_id = sandbox_info.get("sandbox_id")
            if sandbox_id:
                yield sandbox_id

    async def iter_alive_sandbox_info(self) -> AsyncIterator[SandboxInfo]:
        """Yield active sandbox info from the DB."""
        for sandbox_info in await self._db.list_by_in("state", _ACTIVE_STATES):
            yield sandbox_info

    @monitor_metastore_operation
    async def batch_get(self, sandbox_ids: list[str]) -> list[SandboxInfo]:
        """Fetch sandbox info for multiple IDs from the DB. Missing IDs are omitted."""
        if not sandbox_ids:
            return []

        return await self._db.list_by_in("sandbox_id", sandbox_ids)

    @monitor_metastore_operation
    async def list_by(self, field: str, value: str | int | float | bool) -> list[SandboxInfo]:
        """Query sandboxes by *field* == *value* from the DB."""
        return await self._db.list_by(field, value)
