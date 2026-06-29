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
