"""Generic async SQLAlchemy engine provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
        self._engine: AsyncEngine | None = None

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
        connect_args = {"statement_cache_size": 0} if "asyncpg" in self._url else {}
        self._engine = create_async_engine(self._url, echo=False, connect_args=connect_args)

    async def create_tables(self) -> None:
        """Create all tables defined in Base.metadata (idempotent)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        """Dispose of the engine and release all connections."""
        if self._engine is not None:
            await self._engine.dispose()

    @staticmethod
    def _convert_url(url: str) -> str:
        """Convert synchronous database URLs to their async equivalents."""
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            prefix = "postgresql://" if url.startswith("postgresql://") else "postgres://"
            return "postgresql+asyncpg://" + url[len(prefix) :]
        return url
