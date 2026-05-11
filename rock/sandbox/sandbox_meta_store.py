"""SandboxMetaStore - coordinator for Redis (hot path) + DB (query path) dual-write.

Redis remains the source of truth for live sandbox state.
The database is an async replica used for indexed queries (list_by, etc.).
All DB operations are awaited for consistency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import alive_sandbox_key, timeout_sandbox_key
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.metrics.decorator import monitor_metastore_operation
from rock.admin.metrics.monitor import MetricsMonitor

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
    ) -> None:
        self._redis: RedisProvider = redis_provider
        self._db: SandboxTable = sandbox_table
        self.metrics_monitor = MetricsMonitor.create(metric_prefix="meta_store")

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
        """
        await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", sandbox_info)
        if timeout_info is not None:
            await self._redis.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)

        await self._db.create(sandbox_id, sandbox_info, deployment_config)

    @monitor_metastore_operation
    async def update(self, sandbox_id: str, sandbox_info: SandboxInfo) -> None:
        """Merge *sandbox_info* into the existing Redis alive key and await DB update."""
        current = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        merged: dict[str, Any] = {**(current[0] if current else {}), **sandbox_info}
        await self._redis.json_set(alive_sandbox_key(sandbox_id), "$", merged)

        await self._db.update(sandbox_id, sandbox_info)

    @monitor_metastore_operation
    async def delete(self, sandbox_id: str) -> None:
        """Delete Redis alive + timeout keys and await DB delete."""
        await self._redis.json_delete(alive_sandbox_key(sandbox_id))
        await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

        await self._db.delete(sandbox_id)

    @monitor_metastore_operation
    async def archive(self, sandbox_id: str, final_info: SandboxInfo) -> None:
        """Persist final state to DB, then remove sandbox from Redis.

        Unlike ``delete``, the DB record is preserved and updated with
        ``final_info`` (e.g. ``stop_time``, ``state``).  Use this when a
        sandbox has finished its lifecycle and the final state should be
        queryable from the DB.

        The DB write is awaited before the Redis keys are deleted so that
        the final state is always durably stored before the alive key
        disappears.  If the DB write fails the exception propagates and
        Redis cleanup is skipped.
        """
        await self._db.update(sandbox_id, final_info)

        await self._redis.json_delete(alive_sandbox_key(sandbox_id))
        await self._redis.json_delete(timeout_sandbox_key(sandbox_id))

    @monitor_metastore_operation
    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        """Read sandbox info from the Redis alive key."""
        result = await self._redis.json_get(alive_sandbox_key(sandbox_id), "$")
        if result and len(result) > 0:
            return result[0]
        return None

    async def exists(self, sandbox_id: str) -> bool:
        """Return ``True`` when the Redis alive key exists for ``sandbox_id``."""
        return await self.get(sandbox_id) is not None

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
