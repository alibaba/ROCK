"""PostgreSQL snapshot state transition contracts; no migration dependency."""

import asyncio
import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.core.schema import Base
from rock.admin.core.snapshot_table import SnapshotIdentityConflict, SnapshotTable
from rock.admin.service.snapshot_statemachine import InvalidSnapshotTransition, SnapshotStateMachine
from rock.config import DatabaseConfig
from rock.deployments.config import DockerDeploymentConfig

pytestmark = [pytest.mark.integration, pytest.mark.need_docker]


@pytest.mark.asyncio
async def test_postgresql_snapshot_cas_arbitrates_competing_events_without_advisory_locks(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    first = SnapshotTable(provider)
    second = SnapshotTable(provider)
    try:
        record = await first.create_full_snapshot_attempt(
            snapshot_id="competing-events",
            source_sandbox_id="source",
            source_worker_ip="10.0.0.8",
            resource_spec={"vcpus": 2, "memory_mib": 256},
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        machines = [await SnapshotStateMachine.from_record(record, table) for table in (first, second)]
        results = await asyncio.gather(
            machines[0].send("local_ready"), machines[1].send("fail"), return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, dict)]
        losers = [result for result in results if isinstance(result, InvalidSnapshotTransition)]
        assert len(winners) == len(losers) == 1
        assert "concurrent CAS" in str(losers[0])
        assert (await first.get("competing-events"))["state"] == winners[0]["state"]
        # Two stale machines may replay the same delete, but must not replace
        # the winning timestamp or resurrect a terminal catalog entry.
        if winners[0]["state"] == "LOCAL_READY":
            machine = await SnapshotStateMachine.from_record(winners[0], first)
            winners[0] = await machine.send("fail")
        deleters = [await SnapshotStateMachine.from_record(winners[0], table) for table in (first, second)]
        deleted = await asyncio.gather(*(machine.send("delete") for machine in deleters))
        assert deleted[0] == deleted[1]
        assert deleted[0]["state"] == "DELETED"
        assert await first.conditional_transition(
            "competing-events", expected_state="CREATING", new_state="LOCAL_READY",
        ) is None
    finally:
        await provider.close()



@pytest.mark.asyncio
async def test_postgresql_fork_child_inserts_have_one_winner_per_id_without_batch_locks(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    snapshots = SnapshotTable(provider)
    sandboxes = SandboxTable(provider)
    try:
        record = await snapshots.create(
            snapshot_id="desired-base",
            kind="BASE_IMAGE",
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        machine = await SnapshotStateMachine.from_record(record, snapshots)
        await machine.send("local_ready")

        child_ids = ("desired-child-a", "desired-child-b")
        attempts = [(sandbox_id, writer) for sandbox_id in child_ids for writer in ("first", "second")]

        async def create_child(sandbox_id, writer):
            await SandboxTable(provider).create(
                sandbox_id,
                {
                    "sandbox_id": sandbox_id,
                    "user_id": writer,
                    "source_snapshot_id": "desired-base",
                    "namespace": "tenant-e2e",
                    "state": "pending",
                    "image": "snapshot:desired-base",
                    "snapd_node_id": "node-a",
                },
                DockerDeploymentConfig(container_name=sandbox_id, image="snapshot:desired-base"),
            )
            return writer

        statements = []

        def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(provider.engine, "before_cursor_execute", record_statement)
        try:
            results = await asyncio.gather(
                *(create_child(sandbox_id, writer) for sandbox_id, writer in attempts),
                return_exceptions=True,
            )
        finally:
            event.remove(provider.engine, "before_cursor_execute", record_statement)
        assert not any("advisory" in statement or "for update" in statement for statement in statements)
        for index, sandbox_id in enumerate(child_ids):
            outcomes = results[index * 2:index * 2 + 2]
            successful = [result for result in outcomes if isinstance(result, str)]
            failures = [result for result in outcomes if isinstance(result, IntegrityError)]
            assert len(successful) == len(failures) == 1
            original = await sandboxes.get(sandbox_id)
            assert original["user_id"] == successful[0]
            with pytest.raises(IntegrityError):
                await create_child(sandbox_id, "retry")
            assert await sandboxes.get(sandbox_id) == original
        assert len(await sandboxes.list_by("source_snapshot_id", "desired-base")) == 2
    finally:
        await provider.close()



@pytest.mark.asyncio
async def test_postgresql_concurrent_cut_attempts_have_one_id_winner_without_source_serialization(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    first_catalog = SnapshotTable(provider)
    second_catalog = SnapshotTable(provider)
    created_at = datetime.datetime.now(datetime.timezone.utc)

    async def attempt(catalog, snapshot_id):
        return await catalog.create_full_snapshot_attempt(
            snapshot_id=snapshot_id,
            source_sandbox_id="cut-source",
            source_worker_ip="10.0.0.8",
            resource_spec={"vcpus": 2, "memory_mib": 256},
            created_at=created_at,
        )

    try:
        results = await asyncio.gather(
            attempt(first_catalog, "cut-0"),
            attempt(second_catalog, "cut-0"),
            return_exceptions=True,
        )
        successful = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(successful) == len(failures) == 1
        assert isinstance(failures[0], SnapshotIdentityConflict)
        first = successful[0]
        assert "generation" not in first
        competing = await asyncio.gather(
            attempt(first_catalog, "cut-1a"),
            attempt(second_catalog, "cut-1b"),
        )
        assert all(row["state"] == "CREATING" and "generation" not in row for row in competing)
        with pytest.raises(SnapshotIdentityConflict, match="already exists"):
            await attempt(first_catalog, "cut-0")
        assert await first_catalog.get("cut-0") == first

        machine = await SnapshotStateMachine.from_record(first, first_catalog)
        assert (await machine.send("local_ready"))["state"] == "LOCAL_READY"
        # Snapd, not this table, decides which concurrent cuts succeed.
        for other in competing:
            machine = await SnapshotStateMachine.from_record(other, second_catalog)
            assert (await machine.send("local_ready"))["state"] == "LOCAL_READY"
        failed = await attempt(first_catalog, "cut-failed")
        machine = await SnapshotStateMachine.from_record(failed, first_catalog)
        assert (await machine.send("fail"))["state"] == "FAILED"
        restarted = await attempt(first_catalog, "cut-restarted")
        assert restarted["state"] == "CREATING"
        machine = await SnapshotStateMachine.from_record(restarted, first_catalog)
        assert (await machine.send("local_ready"))["state"] == "LOCAL_READY"
        next_attempt = await attempt(first_catalog, "cut-1")
        assert next_attempt["state"] == "CREATING"
    finally:
        await provider.close()
