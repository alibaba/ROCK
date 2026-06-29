"""Generic async SQLAlchemy engine provider."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rock.admin.core.schema import Base
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.config import DatabaseConfig

logger = init_logger(__name__)


class DatabaseProvider:
    """Async SQLAlchemy engine provider.

    Supports SQLite (via ``aiosqlite``) and PostgreSQL (via ``asyncpg``).
    """

    def __init__(self, db_config: DatabaseConfig) -> None:
        self._url = self._convert_url(db_config.url)
        self._pool_size = db_config.pool_size
        self._engine: AsyncEngine | None = None
        self._sync_url = self._convert_sync_url(db_config.url)
        self._sync_engine = None
        self._sync_session: sessionmaker | None = None
        self._db_executor: ThreadPoolExecutor | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("DatabaseProvider not initialised. Call init() first.")
        return self._engine

    async def init(self) -> None:
        """Create the async engine.

        For asyncpg, ``statement_cache_size=0`` prevents
        ``InvalidCachedStatementError`` after external DDL changes
        """
        engine_kwargs: dict[str, object] = {"echo": False}
        if "asyncpg" in self._url:
            engine_kwargs["connect_args"] = {"statement_cache_size": 0}
            engine_kwargs["pool_size"] = self._pool_size
            engine_kwargs["max_overflow"] = 0
            engine_kwargs["pool_timeout"] = 120

        self._engine = create_async_engine(self._url, **engine_kwargs)

        sync_kwargs: dict[str, object] = {"echo": False, "pool_pre_ping": True}
        if self._sync_url.startswith("sqlite"):
            # in-memory/shared: single connection shared across threads, else each
            # worker thread gets its own empty in-memory database
            sync_kwargs["poolclass"] = StaticPool
            sync_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            sync_kwargs["pool_size"] = self._pool_size
            sync_kwargs["max_overflow"] = 0
            sync_kwargs["pool_timeout"] = 120

        self._sync_engine = create_engine(self._sync_url, **sync_kwargs)
        self._sync_session = sessionmaker(bind=self._sync_engine, class_=Session, expire_on_commit=False)
        self._db_executor = ThreadPoolExecutor(max_workers=self._pool_size, thread_name_prefix="db-sync")

    async def create_tables(self) -> None:
        """Create all tables on both async and sync engines (idempotent)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Base.metadata.create_all(self._sync_engine)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        if self._sync_engine is not None:
            self._sync_engine.dispose()
        if self._db_executor is not None:
            self._db_executor.shutdown(wait=False)

    @staticmethod
    def _convert_url(url: str) -> str:
        """Convert synchronous database URLs to their async equivalents."""
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            prefix = "postgresql://" if url.startswith("postgresql://") else "postgres://"
            return "postgresql+asyncpg://" + url[len(prefix) :]
        return url

    @staticmethod
    def _convert_sync_url(url: str) -> str:
        """Convert a DB URL to its synchronous-driver form (psycopg2 / stdlib sqlite)."""
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            prefix = "postgresql://" if url.startswith("postgresql://") else "postgres://"
            return "postgresql+psycopg2://" + url[len(prefix) :]
        return url  # sqlite:/// stays as-is (stdlib sqlite3)
