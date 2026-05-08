"""TDD: DatabaseProvider must tolerate external DDL without errors.

Production scenario: after external ``ALTER TABLE ... ALTER COLUMN TYPE``,
the next query via ``SandboxTable.list_by_in`` raises
``InvalidCachedStatementError`` because asyncpg's prepared statement cache
holds a stale plan, and ``AsyncSession``'s implicit transaction prevents
asyncpg's auto-retry.

RED   — DatabaseProvider without ``statement_cache_size=0`` → error after DDL.
GREEN — DatabaseProvider sets ``statement_cache_size=0`` → no error.
"""

from __future__ import annotations

import asyncpg
import pytest

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import SandboxTable
from rock.config import DatabaseConfig


@pytest.mark.need_docker
class TestBatchGetAfterDDL:
    """Reproduce and fix InvalidCachedStatementError on the sandboxes/batch code path."""

    @pytest.fixture
    async def setup(self, pg_container):
        """Create tables via ORM and yield SandboxTable + pg url."""
        provider = DatabaseProvider(db_config=DatabaseConfig(url=pg_container["url"]))
        await provider.init()
        await provider.create_tables()
        table = SandboxTable(provider)
        yield table, pg_container["url"]
        await provider.close()

    async def test_list_by_in_after_alter_column(self, setup):
        """SandboxTable.list_by_in must work after external ALTER COLUMN TYPE.

        1. Create records and query (warms asyncpg statement cache).
        2. External hotfix: ALTER COLUMN image TYPE VARCHAR(1024).
        3. Same list_by_in query must succeed, not raise InvalidCachedStatementError.
        """
        table, pg_url = setup

        # 1. populate and query — warms prepared statement cache
        ids = [f"cache-{i:03d}" for i in range(10)]
        for sid in ids:
            await table.create(
                sid,
                {
                    "image": "python:3.11",
                    "create_time": "2026-04-14T00:00:00Z",
                },
            )
        records = await table.list_by_in("sandbox_id", ids)
        assert len(records) == 10

        # 2. external DDL — simulates hotfix applied while app is running
        raw = await asyncpg.connect(pg_url)
        try:
            await raw.execute("ALTER TABLE sandbox_record ALTER COLUMN image TYPE VARCHAR(1024)")
        finally:
            await raw.close()

        # 3. same query — must not raise InvalidCachedStatementError
        records = await table.list_by_in("sandbox_id", ids)
        assert len(records) == 10
