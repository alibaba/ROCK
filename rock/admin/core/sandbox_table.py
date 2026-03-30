"""SandboxTable: sandbox-specific CRUD and query operations over DatabaseProvider."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast, get_type_hints

from sqlalchemy import select

from rock.actions.sandbox._generated_types import SandboxInfoField
from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.schema import SandboxRecord  # LIST_BY_ALLOWLIST lives here
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.actions.sandbox.sandbox_info import SandboxInfo

logger = init_logger(__name__)

# ---------------------------------------------------------------------------
# SandboxTable
# ---------------------------------------------------------------------------


class SandboxTable:
    """Sandbox-specific database access layer backed by DatabaseProvider.

    All CRUD and query methods operate on the ``sandbox_record`` table.
    """

    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    async def create(self, sandbox_id: str, data: SandboxInfo) -> None:
        """Insert a new sandbox record.  Raises ``IntegrityError`` if ``sandbox_id`` already exists."""
        filtered = self._filter_data(data)

        # Ensure NOT NULL columns have values when creating a new record.
        for col, default in SandboxRecord._NOT_NULL_DEFAULTS.items():
            if col not in filtered:
                filtered[col] = default

        record = SandboxRecord(sandbox_id=sandbox_id, **filtered)
        async with self._db.session() as session:
            session.add(record)
            await session.commit()

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        """Return a sandbox row as SandboxInfo, or ``None`` if not found."""
        async with self._db.session() as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is None:
                return None
            return _record_to_sandbox_info(record)

    async def update(self, sandbox_id: str, data: SandboxInfo) -> None:
        """Partial update of an existing sandbox record."""
        filtered = self._filter_data(data)
        if not filtered:
            return

        async with self._db.session() as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is None:
                logger.warning("update: sandbox_id=%s not found", sandbox_id)
                return
            for key, value in filtered.items():
                setattr(record, key, value)
            await session.commit()

    async def delete(self, sandbox_id: str) -> None:
        """Hard-delete a sandbox record."""
        async with self._db.session() as session:
            record = await session.get(SandboxRecord, sandbox_id)
            if record is not None:
                await session.delete(record)
                await session.commit()

    async def list_by(self, column: SandboxInfoField, value: str | int | float | bool) -> list[SandboxInfo]:
        """Equality query on a single column. Only columns in ``SandboxRecord.LIST_BY_ALLOWLIST`` are permitted."""
        if column not in SandboxRecord.LIST_BY_ALLOWLIST:
            raise ValueError(f"Querying by column '{column}' is not allowed")

        col_attr = getattr(SandboxRecord, column)
        stmt = select(SandboxRecord).where(col_attr == value)
        async with self._db.session() as session:
            result = await session.execute(stmt)
            return [_record_to_sandbox_info(r) for r in result.scalars().all()]

    async def list_by_in(
        self, column: SandboxInfoField, values: list[str | int | float | bool]
    ) -> list[SandboxInfo]:
        """IN query on a single column. Only columns in ``SandboxRecord.LIST_BY_ALLOWLIST`` are permitted."""
        if column not in SandboxRecord.LIST_BY_ALLOWLIST:
            raise ValueError(f"Querying by column '{column}' is not allowed")
        if not values:
            return []

        col_attr = getattr(SandboxRecord, column)
        stmt = select(SandboxRecord).where(col_attr.in_(values))
        async with self._db.session() as session:
            result = await session.execute(stmt)
            return [_record_to_sandbox_info(r) for r in result.scalars().all()]

    def _filter_data(self, data: SandboxInfo) -> dict[str, Any]:
        """Keep only keys that correspond to actual table columns, excluding ``sandbox_id``."""
        columns = SandboxRecord.column_names()
        return {k: v for k, v in data.items() if k in columns and k != "sandbox_id"}


@lru_cache(maxsize=1)
def _sandbox_info_allowed_keys() -> frozenset[str]:
    """Return the set of valid SandboxInfo field names (cached after first call)."""
    from rock.actions.sandbox.sandbox_info import SandboxInfo as _SI  # local to avoid cycle

    return frozenset(get_type_hints(_SI).keys())


def _record_to_sandbox_info(record: SandboxRecord) -> SandboxInfo:
    """Map ORM row to ``SandboxInfo`` (runtime value is a plain ``dict``)."""
    data = record.to_dict()
    return cast("SandboxInfo", {k: v for k, v in data.items() if k in _sandbox_info_allowed_keys()})
