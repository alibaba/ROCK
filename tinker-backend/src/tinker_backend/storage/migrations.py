"""Small compatibility migrations for early development databases."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure_compatible_schema(conn: AsyncConnection) -> None:
    """Patch existing dev databases that predate the current ORM models.

    The project does not use Alembic yet. `metadata.create_all()` creates new
    tables but does not add columns to existing ones, so keep this narrowly
    scoped to columns added during the train-control iteration.
    """
    if conn.dialect.name == "sqlite":
        result = await conn.execute(text("PRAGMA table_info(operation_futures)"))
        future_columns = {row._mapping["name"] for row in result}
        if "model_id" not in future_columns:
            await conn.execute(text("ALTER TABLE operation_futures ADD COLUMN model_id VARCHAR"))

        table_exists = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='task_environments'")
        )
        if table_exists.first() is not None:
            result = await conn.execute(text("PRAGMA table_info(task_environments)"))
            env_columns = {row._mapping["name"] for row in result}
            for column in ("task_id", "dataset", "split"):
                if column not in env_columns:
                    await conn.execute(text(f"ALTER TABLE task_environments ADD COLUMN {column} VARCHAR DEFAULT ''"))
    else:
        await conn.execute(text("ALTER TABLE operation_futures ADD COLUMN IF NOT EXISTS model_id VARCHAR"))
        await conn.execute(text("ALTER TABLE task_environments ADD COLUMN IF NOT EXISTS task_id VARCHAR DEFAULT ''"))
        await conn.execute(text("ALTER TABLE task_environments ADD COLUMN IF NOT EXISTS dataset VARCHAR DEFAULT ''"))
        await conn.execute(text("ALTER TABLE task_environments ADD COLUMN IF NOT EXISTS split VARCHAR DEFAULT ''"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_operation_futures_model_id ON operation_futures (model_id)")
    )
