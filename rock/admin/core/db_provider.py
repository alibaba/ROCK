"""Generic async SQLAlchemy engine/session provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rock.admin.core.schema import Base
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.config import DatabaseConfig

logger = init_logger(__name__)


class DatabaseProvider:
    """Async SQLAlchemy engine and session factory.

    Supports SQLite (via ``aiosqlite``) and PostgreSQL (via ``asyncpg``).
    """

    def __init__(self, db_config: DatabaseConfig) -> None:
        self._url = self._convert_url(db_config.url)
        self._engine = None
        self._session_factory = None

    async def init_pool(self) -> None:
        """Create the async engine, session factory, and ensure tables exist."""
        logger.info("Initializing database connection pool ...")
        self._engine = create_async_engine(self._url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database connection pool initialised; tables created.")

    async def close_pool(self) -> None:
        """Dispose of the engine and release all connections."""
        if self._engine is not None:
            logger.info("Closing database connection pool ...")
            await self._engine.dispose()
            logger.info("Database connection pool closed.")

    def session(self) -> AsyncSession:
        """Return a new async session context manager."""
        if self._session_factory is None:
            raise RuntimeError("DatabaseProvider not initialised. Call init_pool() first.")
        return self._session_factory()

    @staticmethod
    def _convert_url(url: str) -> str:
        """Convert synchronous database URLs to their async equivalents.

        URLs that already include a driver specifier (e.g. ``sqlite+aiosqlite://``,
        ``postgresql+asyncpg://``) are returned unchanged.
        """
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            # "postgres://" is the Heroku-style shorthand; both map to asyncpg.
            prefix = "postgresql://" if url.startswith("postgresql://") else "postgres://"
            return "postgresql+asyncpg://" + url[len(prefix):]
        # Pass through URLs that already include a driver specifier
        # (e.g. "postgresql+asyncpg://", "sqlite+aiosqlite://").
        return url
