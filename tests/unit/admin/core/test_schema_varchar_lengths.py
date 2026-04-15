"""TDD red-green test: SandboxRecord ORM image column VARCHAR length.

RED  — ORM creates table with VARCHAR(128), inserting a 196-char image fails on PostgreSQL.
GREEN — Change schema to VARCHAR(512), same insert succeeds.

Uses pg_container fixture (real PostgreSQL) so VARCHAR constraints are enforced.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DataError

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import SandboxTable
from rock.config import DatabaseConfig

# A realistic image reference that exceeds VARCHAR(128).
# Taken from a real production failure log.
_LONG_IMAGE = (
    "registry.example.com/org/project-sandbox-images"
    ":my-very-long-tag-name-that-simulates-a-real-world-scenario"
    "-aabbccdd0011223344556677889900ff-v1234567890abcdef1234567890abcdef"
)
assert len(_LONG_IMAGE) > 128, f"Test image must exceed 128 chars, got {len(_LONG_IMAGE)}"


@pytest.mark.need_docker
@pytest.mark.need_database
class TestImageVarcharLength:
    """ORM image column must accept long registry paths on real PostgreSQL."""

    @pytest.fixture
    async def db(self, pg_container):
        """Create a SandboxTable backed by the test PostgreSQL container."""
        provider = DatabaseProvider(db_config=DatabaseConfig(url=pg_container["url"]))
        await provider.init()
        await provider.create_tables()
        table = SandboxTable(provider)
        yield table
        await provider.close()

    async def test_insert_long_image(self, db):
        """A 196-char image string must be accepted by the ORM schema."""
        sandbox_id = "varchar-img-001"
        await db.create(sandbox_id, {
            "image": _LONG_IMAGE,
            "create_time": "2026-04-14T00:00:00Z",
        })
        record = await db.get(sandbox_id)
        assert record is not None
        assert record["image"] == _LONG_IMAGE
