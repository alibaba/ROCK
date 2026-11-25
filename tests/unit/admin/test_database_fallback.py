from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import inspect

from rock import env_vars
from rock.admin.main import _init_database_provider
from rock.config import RockConfig


async def test_snapd_operator_accepts_complete_in_memory_sqlite_without_database_url():
    config = RockConfig()
    config.runtime.operator_type = "snapd"
    config.database.url = ""

    provider = await _init_database_provider(config)
    try:
        assert provider.engine.dialect.name == "sqlite"
        assert provider.engine.url.database == ":memory:"
        assert {
            "sandbox_record",
            "scheduler_task",
            "rock_snapshot",
            "template",
        } <= set(inspect(provider.engine).get_table_names())
        assert "rock_base_image_request" not in inspect(provider.engine).get_table_names()
    finally:
        await provider.close()


async def test_configured_snapd_database_startup_only_initializes_connection(monkeypatch):
    config = RockConfig()
    config.runtime.operator_type = "snapd"
    config.database.url = "postgresql://rock:test@database/rock"
    provider = SimpleNamespace(
        init=AsyncMock(),
        create_tables=AsyncMock(),
        engine=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(env_vars, "ROCK_ADMIN_ROLE", "admin")
    monkeypatch.setattr("rock.admin.main.DatabaseProvider", lambda **_: provider)

    assert await _init_database_provider(config) is provider
    provider.init.assert_awaited_once_with()
    provider.create_tables.assert_not_awaited()
