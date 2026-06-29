import asyncio
import threading
import time

import pytest

from rock.admin.core.db_provider import DatabaseProvider
from rock.config import DatabaseConfig


def test_database_config_has_pool_size_default():
    cfg = DatabaseConfig(url="")
    assert cfg.pool_size == 100


def test_database_config_pool_size_override():
    cfg = DatabaseConfig(url="", pool_size=10)
    assert cfg.pool_size == 10


@pytest.mark.asyncio
async def test_engine_uses_configured_pool_size_for_postgres(monkeypatch):
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return object()

    monkeypatch.setattr("rock.admin.core.db_provider.create_async_engine", fake_create_async_engine)
    monkeypatch.setattr("rock.admin.core.db_provider.create_engine", lambda url, **k: object())
    provider = DatabaseProvider(db_config=DatabaseConfig(url="postgresql://u:p@h:5432/db", pool_size=7))
    await provider.init()
    assert captured["pool_size"] == 7
    assert captured["max_overflow"] == 0


def test_convert_sync_url_postgres():
    assert DatabaseProvider._convert_sync_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"
    assert DatabaseProvider._convert_sync_url("postgres://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"


def test_convert_sync_url_sqlite_unchanged():
    assert DatabaseProvider._convert_sync_url("sqlite:///:memory:") == "sqlite:///:memory:"
    assert DatabaseProvider._convert_sync_url("sqlite:///x.db") == "sqlite:///x.db"


@pytest.mark.asyncio
async def test_sync_engine_uses_pool_size_and_pre_ping_for_postgres(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return object()

    monkeypatch.setattr("rock.admin.core.db_provider.create_async_engine", lambda url, **k: object())
    monkeypatch.setattr("rock.admin.core.db_provider.create_engine", fake_create_engine)

    provider = DatabaseProvider(db_config=DatabaseConfig(url="postgresql://u:p@h:5432/db", pool_size=7))
    await provider.init()

    assert captured["url"] == "postgresql+psycopg2://u:p@h:5432/db"
    assert captured["pool_size"] == 7
    assert captured["max_overflow"] == 0
    assert captured["pool_pre_ping"] is True
    assert provider._db_executor._max_workers == 7


@pytest.mark.asyncio
async def test_init_sets_up_sync_session_and_executor_for_sqlite():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:", pool_size=4))
    await provider.init()
    assert provider._sync_session is not None
    assert provider._db_executor is not None
    await provider.close()


@pytest.mark.asyncio
async def test_run_in_session_executes_in_db_sync_thread():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:", pool_size=4))
    await provider.init()
    try:
        name = await provider.run_in_session(lambda s: threading.current_thread().name)
        assert name.startswith("db-sync")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_run_in_session_returns_value_and_propagates_errors():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:", pool_size=4))
    await provider.init()
    try:
        assert await provider.run_in_session(lambda s: 42) == 42
        with pytest.raises(ValueError, match="boom"):
            await provider.run_in_session(lambda s: (_ for _ in ()).throw(ValueError("boom")))
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_run_in_session_runs_concurrently_on_distinct_threads():
    provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:", pool_size=4))
    await provider.init()
    try:

        def slow(_s):
            time.sleep(0.2)
            return threading.current_thread().name

        results = await asyncio.gather(
            provider.run_in_session(slow),
            provider.run_in_session(slow),
        )
        assert len(set(results)) == 2  # two calls land on different worker threads
    finally:
        await provider.close()
