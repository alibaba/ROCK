from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tinker_backend.config import Settings
from tinker_backend.services.observability_service import collect_metrics_snapshot
from tinker_backend.storage.engine import build_database_url
from tinker_backend.storage.migrations import ensure_compatible_schema
from tinker_backend.storage.models import Base


class SQLiteStorageConfigTest(unittest.TestCase):
    def test_default_database_url_uses_repo_local_sqlite(self) -> None:
        settings = Settings(
            _env_file=None,
            database_url=None,
            postgres_host=None,
            postgres_database=None,
            postgres_user=None,
            postgres_password=None,
        )

        url = make_url(build_database_url(settings))

        self.assertEqual(url.drivername, "sqlite+aiosqlite")
        self.assertEqual(Path(url.database or "").name, "tinker_backend.sqlite3")
        self.assertEqual(Path(url.database or "").parent.name, "tinker-backend")

    def test_explicit_postgres_database_url_is_preserved(self) -> None:
        settings = Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://user:pass@example.com:5432/tinker_backend",
        )

        url = make_url(build_database_url(settings))

        self.assertEqual(url.drivername, "postgresql+asyncpg")
        self.assertEqual(url.host, "example.com")


class SQLiteMigrationTest(unittest.TestCase):
    def test_sqlite_compat_migration_adds_model_id_idempotently(self) -> None:
        async def run() -> None:
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            create table operation_futures (
                                request_id integer primary key,
                                request_type varchar,
                                status varchar
                            )
                            """
                        )
                    )
                    await ensure_compatible_schema(conn)
                    await ensure_compatible_schema(conn)
                    result = await conn.execute(text("pragma table_info(operation_futures)"))
                    columns = {row._mapping["name"] for row in result}
            finally:
                await engine.dispose()

            self.assertIn("model_id", columns)

        asyncio.run(run())


class SQLiteObservabilityTest(unittest.TestCase):
    def test_sqlite_metrics_queries_do_not_use_postgres_sql(self) -> None:
        async def run() -> dict:
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    return await collect_metrics_snapshot(session)
            finally:
                await engine.dispose()

        snapshot = asyncio.run(run())

        self.assertEqual(snapshot["totals"]["runtimes"], 0)
        self.assertEqual(snapshot["stale_claimed_actions"], [])


if __name__ == "__main__":
    unittest.main()
