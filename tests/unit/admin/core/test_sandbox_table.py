"""Tests for SandboxTable — SQLite in-memory (fast) and PostgreSQL (Docker)."""

import pytest
from sqlalchemy.exc import IntegrityError

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import SandboxTable
from rock.config import DatabaseConfig


class TestSandboxTableWithSQLite:
    """Unit tests for SandboxTable using an in-memory SQLite database.

    These tests cover all CRUD paths and run without any external dependencies.
    """

    @pytest.fixture
    async def db(self):
        provider = DatabaseProvider(db_config=DatabaseConfig(url="sqlite:///:memory:"))
        await provider.init()
        await provider.create_tables()
        table = SandboxTable(provider)
        yield table
        await provider.close()

    async def test_insert_and_get(self, db):
        sandbox_id = "sqlite-sbx-001"
        data = {
            "user_id": "user-1",
            "image": "python:3.11",
            "experiment_id": "exp-1",
            "namespace": "default",
            "cluster_name": "local",
            "state": "running",
            "host_ip": "127.0.0.1",
            "create_time": "2025-01-01T00:00:00Z",
        }
        await db.create(sandbox_id, data)
        record = await db.get(sandbox_id)
        assert record is not None
        assert record["sandbox_id"] == sandbox_id
        assert record["user_id"] == "user-1"
        assert record["state"] == "running"

    async def test_insert_duplicate_raises(self, db):
        sandbox_id = "sqlite-sbx-002"
        data = {"state": "pending", "create_time": "2025-01-01T00:00:00Z"}
        await db.create(sandbox_id, data)
        with pytest.raises(IntegrityError):
            await db.create(sandbox_id, {**data, "state": "running"})

    async def test_update(self, db):
        sandbox_id = "sqlite-sbx-003"
        await db.create(sandbox_id, {"state": "pending", "create_time": "2025-01-01T00:00:00Z"})
        await db.update(sandbox_id, {"state": "running", "host_ip": "10.0.0.2"})
        record = await db.get(sandbox_id)
        assert record["state"] == "running"
        assert record["host_ip"] == "10.0.0.2"

    async def test_delete(self, db):
        sandbox_id = "sqlite-sbx-004"
        await db.create(sandbox_id, {"create_time": "2025-01-01T00:00:00Z"})
        await db.delete(sandbox_id)
        assert await db.get(sandbox_id) is None

    async def test_list_by(self, db):
        await db.create("lb-s1", {"user_id": "alice", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lb-s2", {"user_id": "alice", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lb-s3", {"user_id": "bob", "create_time": "2025-01-01T00:00:00Z"})
        results = await db.list_by("user_id", "alice")
        assert len(results) == 2

    async def test_list_by_in(self, db):
        await db.create("lbi-1", {"user_id": "alice", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lbi-2", {"user_id": "bob", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lbi-3", {"user_id": "carol", "create_time": "2025-01-01T00:00:00Z"})
        results = await db.list_by_in("sandbox_id", ["lbi-1", "lbi-3"])
        assert {r["sandbox_id"] for r in results} == {"lbi-1", "lbi-3"}

    async def test_list_by_rejects_blacklisted_column(self, db):
        with pytest.raises(ValueError, match="not allowed"):
            await db.list_by("phases", "{}")

    async def test_get_nonexistent_returns_none(self, db):
        assert await db.get("does-not-exist") is None

    async def test_update_nonexistent_is_noop(self, db):
        """update() on a non-existent ID should log a warning and not raise."""
        await db.update("does-not-exist", {"state": "running"})  # should not raise

    async def test_not_null_defaults_applied_on_insert(self, db):
        """Insert with minimal data should fill NOT NULL columns from _NOT_NULL_DEFAULTS."""
        sandbox_id = "sqlite-sbx-defaults"
        await db.create(sandbox_id, {})
        record = await db.get(sandbox_id)
        assert record is not None
        assert record["user_id"] == "default"
        assert record["state"] == "pending"


@pytest.mark.need_docker
class TestSandboxTableWithPostgres:
    """Integration tests for SandboxTable using a real PostgreSQL container."""

    @pytest.fixture
    async def db(self, pg_container):
        """Create a SandboxTable connected to the test PostgreSQL container."""
        provider = DatabaseProvider(db_config=DatabaseConfig(url=pg_container["url"]))
        await provider.init()
        await provider.create_tables()
        table = SandboxTable(provider)
        yield table
        await provider.close()

    async def test_fixture_connection(self, db):
        """Verify that the fixture can connect and create tables."""
        assert db._db._engine is not None

    async def test_insert_and_get(self, db):
        sandbox_id = "test-sandbox-001"
        data = {
            "user_id": "user-1",
            "image": "python:3.11",
            "experiment_id": "exp-1",
            "namespace": "default",
            "cluster_name": "local",
            "state": "RUNNING",
            "host_ip": "10.0.0.1",
            "create_time": "2025-01-01T00:00:00Z",
        }

        await db.create(sandbox_id, data)
        record = await db.get(sandbox_id)

        assert record is not None
        assert record["sandbox_id"] == sandbox_id
        assert record["user_id"] == "user-1"
        assert record["state"] == "RUNNING"

    async def test_insert_duplicate_raises(self, db):
        sandbox_id = "test-sandbox-002"
        data = {
            "user_id": "user-1",
            "state": "PENDING",
            "create_time": "2025-01-01T00:00:00Z",
        }
        await db.create(sandbox_id, data)

        with pytest.raises(IntegrityError):
            await db.create(sandbox_id, {**data, "state": "RUNNING"})

    async def test_update(self, db):
        sandbox_id = "test-sandbox-003"
        await db.create(
            sandbox_id,
            {
                "state": "PENDING",
                "create_time": "2025-01-01T00:00:00Z",
            },
        )

        await db.update(sandbox_id, {"state": "RUNNING", "host_ip": "10.0.0.2"})
        record = await db.get(sandbox_id)
        assert record["state"] == "RUNNING"
        assert record["host_ip"] == "10.0.0.2"

    async def test_delete(self, db):
        sandbox_id = "test-sandbox-004"
        await db.create(sandbox_id, {"create_time": "2025-01-01T00:00:00Z"})
        await db.delete(sandbox_id)
        assert await db.get(sandbox_id) is None

    async def test_list_by(self, db):
        await db.create("lb-1", {"user_id": "alice", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lb-2", {"user_id": "alice", "create_time": "2025-01-01T00:00:00Z"})
        await db.create("lb-3", {"user_id": "bob", "create_time": "2025-01-01T00:00:00Z"})

        results = await db.list_by("user_id", "alice")
        assert len(results) == 2

    async def test_list_by_rejects_blacklisted_column(self, db):
        with pytest.raises(ValueError, match="not allowed"):
            await db.list_by("phases", "{}")

    async def test_json_fields_postgresql(self, db):
        """Verify JSONB variant works correctly on PostgreSQL."""
        sandbox_id = "json-test-001"
        data = {
            "create_time": "2025-01-01T00:00:00Z",
            "phases": {"build": "done", "deploy": "pending"},
            "port_mapping": {"8080": 30080, "22": 30022},
        }
        await db.create(sandbox_id, data)
        record = await db.get(sandbox_id)

        assert record["phases"] == {"build": "done", "deploy": "pending"}
        assert record["port_mapping"] == {"8080": 30080, "22": 30022}

    async def test_get_nonexistent_returns_none(self, db):
        assert await db.get("does-not-exist") is None
