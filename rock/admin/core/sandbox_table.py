"""SandboxTable: sandbox-specific CRUD and query operations over DatabaseProvider."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.schema import SandboxRecord
from rock.admin.metrics.decorator import monitor_metastore_operation
from rock.admin.metrics.monitor import MetricsMonitor
from rock.config import RockConfig
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.actions.sandbox.sandbox_info import SandboxInfo
    from rock.deployments.config import DockerDeploymentConfig

logger = init_logger(__name__)


_DISCONNECT_RETRY_ATTEMPTS = 4

# Exceptions retried with exponential back-off across DB outages.
# - OperationalError / InterfaceError: SQLAlchemy-wrapped runtime connection
#   problems on the statement-execution path (stale connection, server gone,
#   socket-level failures observed mid-query).
# - DisconnectionError: explicit pool-level "connection is invalid" signal.
# - OSError / ConnectionError / asyncio.TimeoutError: asyncpg's connect path
#   raises these directly; SQLAlchemy does NOT wrap them into DBAPIError
#   because they fire before a statement is ever issued. Without catching
#   them here, retries cannot bridge a multi-second PG restart window.
# Excluded on purpose: DatabaseError (would swallow IntegrityError,
# DataError, ProgrammingError — all permanent failures that must fast-fail).
_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)


def _retry_on_disconnect(func):
    """Retry up to _DISCONNECT_RETRY_ATTEMPTS times across DB outages."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        last_exc: BaseException | None = None
        for attempt in range(1, _DISCONNECT_RETRY_ATTEMPTS + 1):
            try:
                return await func(*args, **kwargs)
            except _RETRY_EXCEPTIONS as exc:
                last_exc = exc
                logger.warning(
                    "DB connection lost on %s (attempt %d/%d): %r",
                    func.__name__,
                    attempt,
                    _DISCONNECT_RETRY_ATTEMPTS,
                    exc,
                )
                if attempt < _DISCONNECT_RETRY_ATTEMPTS:
                    await asyncio.sleep(1.0 * 2 ** (attempt - 1))
        assert last_exc is not None
        raise last_exc

    return wrapper


class SandboxTable:
    """Sandbox-specific database access layer backed by DatabaseProvider.

    All methods use plain ``dict`` for both input and output.

    Write path (create / update):
    - Fields from ``SandboxInfo`` / ``DockerDeploymentConfig`` that match a
      ``SandboxRecord`` column are written to the corresponding scalar column.
    - ``status`` column stores the full ``SandboxInfo`` dict.
    - ``spec`` column stores the full ``DockerDeploymentConfig.model_dump()`` dict.

    Read path (get / list_by / list_by_in):
    - Returns ``record.to_dict()`` — a plain dict with all non-None column values,
      including ``spec`` and ``status``.
    """

    def __init__(self, db_provider: DatabaseProvider, rock_config: RockConfig | None = None) -> None:
        self._db = db_provider
        self.metrics_monitor = MetricsMonitor.create(
            export_interval_millis=20_000,
            metrics_endpoint=rock_config.runtime.metrics_endpoint if rock_config else "",
            user_defined_tags=rock_config.runtime.user_defined_tags if rock_config else {},
            metric_prefix="meta_store.db",
        )

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def create(
        self,
        sandbox_id: str,
        info: SandboxInfo,
        config: DockerDeploymentConfig | None = None,
    ) -> None:
        """Insert a new sandbox record.

        Scalar columns are populated from the union of *config* and *info*
        (``info`` takes priority on conflicts).
        Raises ``IntegrityError`` if ``sandbox_id`` already exists.
        """
        config_dict = config.model_dump() if config is not None else {}
        merged = {**config_dict, **info}
        filtered = _pick_columns(merged)

        for col, default in SandboxRecord._NOT_NULL_DEFAULTS.items():
            if col not in filtered:
                filtered[col] = default

        filtered["status"] = dict(info)
        if config_dict:
            filtered["spec"] = config_dict

        record = SandboxRecord(sandbox_id=sandbox_id, **filtered)
        async with AsyncSession(self._db.engine) as session:
            session.add(record)
            await session.commit()

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def get(self, sandbox_id: str) -> dict | None:
        """Return a sandbox row as a plain dict, or ``None`` if not found."""
        async with AsyncSession(self._db.engine) as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is None:
                return None
            return record.to_dict()

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def update(self, sandbox_id: str, info: SandboxInfo) -> None:
        """Partial update of scalar columns; always overwrites ``status`` with *info*."""
        filtered = _pick_columns(info)
        filtered["status"] = dict(info)

        async with AsyncSession(self._db.engine) as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is None:
                logger.warning("update: sandbox_id=%s not found", sandbox_id)
                return
            for key, value in filtered.items():
                setattr(record, key, value)
            await session.commit()

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def delete(self, sandbox_id: str) -> None:
        """Hard-delete a sandbox record."""
        async with AsyncSession(self._db.engine) as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is not None:
                await session.delete(record)
                await session.commit()

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def list_by(self, column: str, value: str | int | float | bool) -> list[dict]:
        """Equality query on a single column. Only columns in ``SandboxRecord.LIST_BY_ALLOWLIST`` are permitted."""
        if column not in SandboxRecord.LIST_BY_ALLOWLIST:
            raise ValueError(f"Querying by column '{column}' is not allowed")
        col_attr = getattr(SandboxRecord, column)
        stmt = select(SandboxRecord).where(col_attr == value)
        async with AsyncSession(self._db.engine) as session:
            result = await session.execute(stmt)
            return [r.to_dict() for r in result.scalars().all()]

    @_retry_on_disconnect
    @monitor_metastore_operation
    async def list_by_in(self, column: str, values: list[str | int | float | bool]) -> list[dict]:
        """IN query on a single column. Only columns in ``SandboxRecord.LIST_BY_ALLOWLIST`` are permitted."""
        if column not in SandboxRecord.LIST_BY_ALLOWLIST:
            raise ValueError(f"Querying by column '{column}' is not allowed")
        if not values:
            return []
        col_attr = getattr(SandboxRecord, column)
        stmt = select(SandboxRecord).where(col_attr.in_(values))
        async with AsyncSession(self._db.engine) as session:
            result = await session.execute(stmt)
            return [r.to_dict() for r in result.scalars().all()]


def _pick_columns(data: dict[str, Any]) -> dict[str, Any]:
    """Return only keys matching a scalar SandboxRecord column, excluding sandbox_id/spec/status."""
    columns = SandboxRecord.column_names() - {"sandbox_id", "spec", "status"}
    return {k: v for k, v in data.items() if k in columns}
