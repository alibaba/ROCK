"""PostgreSQL snapshot database contracts; no migration dependency."""

import asyncio
import datetime

import pytest
from sqlalchemy import inspect, text

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.core.schema import Base
from rock.admin.core.snapshot_table import BaseImageResolutionIdentity, SnapshotIdentityConflict, SnapshotTable
from rock.config import DatabaseConfig
from rock.deployments.config import DockerDeploymentConfig

pytestmark = [pytest.mark.integration, pytest.mark.need_docker]


@pytest.mark.asyncio
async def test_postgresql_base_image_build_key_claim_is_cross_replica_singleton(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    first = SnapshotTable(provider)
    second = SnapshotTable(provider)
    digest = "sha256:" + "a" * 64
    build_key = "sha256:" + "b" * 64

    async def claim(table: SnapshotTable, identity: str):
        return await table.claim_base_image(
            snapshot_id=identity,
            resolution=BaseImageResolutionIdentity(
                oci_ref=f"registry.example/image:{identity}",
                oci_digest=digest,
                oci_platform="linux/amd64",
                base_image_builder_version="test-builder-v1",
                base_image_build_key=build_key,
                source_worker_ip="10.0.0.8",
            ),
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )

    try:
        claims = await asyncio.gather(claim(first, "base-a"), claim(second, "base-b"))
        canonical_ids = {claim.snapshot["snapshot_id"] for claim in claims}
        assert len(canonical_ids) == 1
        with provider.engine.begin() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM rock_snapshot WHERE base_image_build_key=:key"),
                {"key": build_key},
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM template"),
            ).scalar_one() == 0
            assert "rock_base_image_request" not in inspect(connection).get_table_names()
    finally:
        await provider.close()



@pytest.mark.asyncio
async def test_postgresql_draft_concurrent_builds_preserve_snapshot_identity(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    try:
        snapshots = SnapshotTable(provider)
        await snapshots.create(
            snapshot_id="e2b-preallocated", kind="BASE_IMAGE",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            resource_spec={"vcpus": 4, "memory_mib": 8192, "disk_size_mb": 51200},
            product_metadata={"names": ["E2B"]},
        )

        async def claim(digit):
            return await snapshots.claim_base_image(
                snapshot_id="e2b-preallocated",
                resolution=BaseImageResolutionIdentity(
                    oci_ref=f"registry/image:{digit}", oci_digest="sha256:" + digit * 64,
                    oci_platform="linux/amd64", base_image_builder_version="test-builder-v1",
                    base_image_build_key="sha256:" + digit * 64, source_worker_ip="10.0.0.8",
                ),
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )

        results = await asyncio.gather(claim("a"), claim("b"), return_exceptions=True)
        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, SnapshotIdentityConflict)]
        assert len(successes) == len(failures) == 1
        final = await snapshots.get("e2b-preallocated")
        assert final["snapshot_id"] == "e2b-preallocated"
        assert final["oci_digest"] == successes[0].snapshot["oci_digest"]
        assert final["oci_ref"] == successes[0].snapshot["oci_ref"]
        assert final["product_metadata"] == {"names": ["E2B"]}
        assert final["resource_spec"] == {"vcpus": 4, "memory_mib": 8192, "disk_size_mb": 51200}
    finally:
        await provider.close()



@pytest.mark.asyncio
async def test_postgresql_allocated_base_images_keep_their_ids_for_identical_oci_content(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    snapshots = SnapshotTable(provider)
    resolution = BaseImageResolutionIdentity(
        oci_ref="registry/image:same", oci_digest="sha256:" + "a" * 64,
        oci_platform="linux/amd64", base_image_builder_version="test-builder-v1",
        base_image_build_key="sha256:" + "b" * 64, source_worker_ip="10.0.0.8",
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        canonical = await snapshots.claim_base_image(snapshot_id="native", resolution=resolution, created_at=now)
        assert canonical.snapshot["snapshot_id"] == "native"
        for snapshot_id in ("e2b-first", "e2b-second"):
            await snapshots.create(
                snapshot_id=snapshot_id, kind="BASE_IMAGE", created_at=now,
                product_metadata={"names": [snapshot_id]}, resource_spec={"vcpus": 4},
            )
            claimed = await snapshots.claim_base_image(
                snapshot_id=snapshot_id, resolution=resolution, created_at=now,
            )
            assert claimed.snapshot["snapshot_id"] == snapshot_id
            assert claimed.snapshot["product_metadata"] == {"names": [snapshot_id]}
            assert claimed.snapshot["resource_spec"] == {"vcpus": 4}
        joined = await snapshots.claim_base_image(snapshot_id="native-retry", resolution=resolution, created_at=now)
        assert joined.snapshot["snapshot_id"] == "native"
        with provider.engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM rock_snapshot")).scalar_one() == 3
            assert connection.execute(text("SELECT count(*) FROM template")).scalar_one() == 0
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_postgresql_stopped_checkpoint_references_and_restart_rebind(snapshot_postgres_url):
    provider = DatabaseProvider(DatabaseConfig(url=snapshot_postgres_url))
    await provider.init()
    with provider.engine.begin() as connection:
        Base.metadata.create_all(connection)
    snapshots = SnapshotTable(provider)
    sandboxes = SandboxTable(provider)
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        await snapshots.create(snapshot_id="base", kind="BASE_IMAGE", created_at=now)
        await snapshots.create(
            snapshot_id="pause-cut", kind="FULL", created_at=now, source_sandbox_id="stopped-owner",
        )
        for state in ("running", "pending", "stopped", "archived", "deleted", "future-state"):
            sandbox_id = f"{state}-owner"
            await sandboxes.create(
                sandbox_id,
                {"state": state, "source_snapshot_id": "base", "pause_snapshot_id": "pause-cut"},
                DockerDeploymentConfig(container_name=sandbox_id, source_snapshot_id="base"),
            )
        expected_owners = {
            "running-owner", "pending-owner", "stopped-owner", "archived-owner", "deleted-owner", "future-state-owner",
        }
        for snapshot_id in ("base", "pause-cut"):
            assert {row["sandbox_id"] for row in await sandboxes.list_snapshot_references(snapshot_id)} == expected_owners

        info = await sandboxes.get("stopped-owner")
        info["state"] = "pending"
        info["source_snapshot_id"] = "pause-cut"
        info["spec"] = {**info["spec"], "source_snapshot_id": "pause-cut"}
        await sandboxes.persist_lifecycle("stopped-owner", info)
        durable = await sandboxes.get("stopped-owner")
        assert durable["state"] == "pending"
        assert durable["source_snapshot_id"] == durable["spec"]["source_snapshot_id"] == "pause-cut"
        assert durable["pause_snapshot_id"] == "pause-cut"

        original = await sandboxes.get("running-owner")
        attempted = {
            **original, "source_snapshot_id": "pause-cut", "spec": {**original["spec"], "source_snapshot_id": "pause-cut"},
        }
        with pytest.raises(ValueError, match="only a stopped sandbox"):
            await sandboxes.persist_lifecycle("running-owner", attempted)
        assert await sandboxes.get("running-owner") == original

        await sandboxes.update("stopped-owner", {"state": "deleted", "pause_snapshot_id": "pause-cut"})
        references = await sandboxes.list_snapshot_references("pause-cut")
        assert next(row for row in references if row["sandbox_id"] == "stopped-owner")["state"] == "deleted"
    finally:
        await provider.close()
