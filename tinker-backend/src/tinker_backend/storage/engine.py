"""Async SQLAlchemy engine and session management."""

from collections.abc import AsyncGenerator
import os
from pathlib import Path

from sqlalchemy import URL, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from tinker_backend.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Keep checkout defaults; installed wheels use a writable user state directory.
_STATE_ROOT = (
    _REPO_ROOT if (_REPO_ROOT / "src" / "tinker_backend").is_dir()
    else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "tinker-backend"
)
_DEFAULT_SQLITE_PATH = _STATE_ROOT / "tinker_backend.sqlite3"


def build_database_url(settings: Settings) -> str:
    if settings.database_url is not None and settings.database_url.get_secret_value():
        return settings.database_url.get_secret_value()

    postgres_password = settings.postgres_password.get_secret_value() if settings.postgres_password else None
    if all([settings.postgres_host, settings.postgres_database, settings.postgres_user, postgres_password]):
        url = URL.create(
            "postgresql+asyncpg",
            username=settings.postgres_user,
            password=postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_database,
        )
        return url.render_as_string(hide_password=False)

    return f"sqlite+aiosqlite:///{_DEFAULT_SQLITE_PATH}"


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def _sqlite_path_from_url(database_url: str) -> Path | None:
    if not _is_sqlite_url(database_url):
        return None
    parsed = make_url(database_url)
    if parsed.database in (None, "", ":memory:"):
        return None
    return Path(parsed.database)


def _sqlite_connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if _is_sqlite_url(database_url) else {}


def _configure_sqlite_pragmas(engine: AsyncEngine) -> None:
    if engine.sync_engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        database_url = build_database_url(settings)
        sqlite_path = _sqlite_path_from_url(database_url)
        if sqlite_path is not None:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            database_url,
            echo=settings.sql_echo,
            pool_pre_ping=True,
            connect_args=_sqlite_connect_args(database_url),
        )
        _configure_sqlite_pragmas(_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
