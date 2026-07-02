"""Sync SQLAlchemy engine provider, executed off the event loop.

DB work (asyncpg-style codec decode / ORM materialization / flush) is pure CPU
that, under an async driver, runs on the asyncio event-loop thread and starves
every other coroutine under high write concurrency. Here the engine is
*synchronous* (psycopg2) and every DB call is dispatched to a dedicated thread
pool via :meth:`DatabaseProvider.run`, so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rock.admin.core.schema import Base
from rock.logger import init_logger
from rock.utils.concurrent_helper import create_db_executor

if TYPE_CHECKING:
    from rock.config import DatabaseConfig

logger = init_logger(__name__)

T = TypeVar("T")

# Wait time (s) for a free connection when the pool is saturated.
_PG_POOL_TIMEOUT = 120


class DatabaseProvider:
    """Sync SQLAlchemy engine provider; all DB calls run on a dedicated thread pool.

    Supports SQLite (sync) and PostgreSQL (via psycopg2).
    """

    def __init__(self, db_config: DatabaseConfig) -> None:
        self._url = self._convert_url(db_config.url)
        self._pool_size = db_config.pool_size
        self._max_overflow = db_config.max_overflow
        self._engine: Engine | None = None
        self._session_factory: sessionmaker | None = None
        self._executor: ThreadPoolExecutor | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("DatabaseProvider not initialised. Call init() first.")
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        if self._session_factory is None:
            raise RuntimeError("DatabaseProvider not initialised. Call init() first.")
        return self._session_factory

    async def init(self) -> None:
        """Create the sync engine, session factory, and the dedicated DB thread pool."""
        # pool_pre_ping: validate a connection with a cheap SELECT 1 on checkout so
        # stale connections (PG restart / idle-killed) are transparently recycled.
        engine_kwargs: dict[str, object] = {"echo": False, "pool_pre_ping": True}
        if self._url.startswith("sqlite"):
            # In-memory / file SQLite: share one connection across the pool so
            # every worker thread sees the same database, and allow cross-thread use.
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs["pool_size"] = self._pool_size
            engine_kwargs["max_overflow"] = self._max_overflow
            engine_kwargs["pool_timeout"] = _PG_POOL_TIMEOUT

        self._engine = create_engine(self._url, **engine_kwargs)
        # expire_on_commit=False: ORM attributes stay usable after commit, so a
        # post-commit to_dict() never triggers a surprise lazy re-SELECT.
        self._session_factory = sessionmaker(bind=self._engine, class_=Session, expire_on_commit=False)

        # Thread pool matches the max connection count so a worker never blocks
        # waiting on a connection slot.
        pool_total = self._pool_size + self._max_overflow
        self._executor = create_db_executor(max_workers=pool_total)
        logger.info(
            "DatabaseProvider initialised: %s (conns<=%d, db thread pool=%d)", self._url, pool_total, pool_total
        )

    async def run(self, fn: Callable[..., T], /, *args, **kwargs) -> T:
        """Run a blocking DB callable on the dedicated thread pool."""
        if self._executor is None:
            raise RuntimeError("DatabaseProvider not initialised. Call init() first.")
        loop = asyncio.get_running_loop()
        if kwargs:
            fn = functools.partial(fn, **kwargs)
        return await loop.run_in_executor(self._executor, fn, *args)

    async def create_tables(self) -> None:
        """Create all tables defined in Base.metadata (idempotent)."""
        with self.engine.begin() as conn:
            Base.metadata.create_all(conn)

    async def close(self) -> None:
        """Dispose of the engine and shut down the thread pool."""
        if self._engine is not None:
            self._engine.dispose()
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    @staticmethod
    def _convert_url(url: str) -> str:
        """Normalise async / bare URLs to their sync (psycopg2 / sqlite) equivalents."""
        if url.startswith("sqlite+aiosqlite:///"):
            return url.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg2://" + url[len(prefix) :]
        return url
