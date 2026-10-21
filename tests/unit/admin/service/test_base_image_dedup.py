import asyncio
import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.schema import RockSnapshotRecord, TemplateRecord
from rock.admin.core.snapshot_table import SnapshotTable
from rock.admin.proto.e2b_snapshot import E2BCreateTemplateRequest, E2BStartTemplateBuildRequest
from rock.admin.proto.snapshot import CreateBaseImageRequest
from rock.admin.service.e2b_snapshot_service import E2BSnapshotService
from rock.admin.service.snapshot_manager import (
    SnapshotConflict,
    SnapshotManager,
    SnapshotReconciler,
    SnapshotResultUnknown,
    _canonical_base_image_build_key,
)
from rock.config import DatabaseConfig
from rock.sandbox.operator.snapd import OperatorResultCode, SnapshotOperationResult


def _build_key(digest: str, platform: str, builder: str) -> str:
    return _canonical_base_image_build_key(digest, platform, builder)


class _BaseImageOperator:
    def __init__(self) -> None:
        self.digests: dict[str, str] = {}
        self.builder_version = "test-oci-ext4-v1"
        self.resolve_requests = []
        self.create_requests = []
        self.actual_builds: set[str] = set()
        self.create_result_state = "CREATING"
        self.lose_create_reply = False
        self.resolution_worker_ip = "10.20.30.40"
        self.create_worker_ips = []
        self.status_worker_ips = []
        self.finalize_worker_ips = []

    async def resolve_base_image(self, request):
        self.resolve_requests.append(request)
        digest = self.digests[request.oci_ref]
        key = _build_key(digest, request.platform, self.builder_version)
        return SnapshotOperationResult(
            key,
            OperatorResultCode.ACCEPTED,
            "CREATING",
            {
                "oci_ref": request.oci_ref,
                "oci_digest": digest,
                "oci_platform": request.platform,
                "base_image_builder_version": self.builder_version,
                "base_image_build_key": key,
                "source_worker_ip": self.resolution_worker_ip,
            },
        )

    async def create_base_image(self, request, *, source_worker_ip):
        self.create_worker_ips.append(source_worker_ip)
        self.create_requests.append(request)
        self.actual_builds.add(request.snapshot_id)
        if self.lose_create_reply:
            return SnapshotOperationResult(
                request.snapshot_id, OperatorResultCode.RESULT_UNKNOWN, "CREATING", {}, "lost create reply"
            )
        return SnapshotOperationResult(
            request.snapshot_id,
            (
                OperatorResultCode.FAILED
                if self.create_result_state == "FAILED"
                else OperatorResultCode.ACCEPTED
            ),
            self.create_result_state,
            {"identity": request.snapshot_id, "state": self.create_result_state},
            "terminal build failure" if self.create_result_state == "FAILED" else None,
        )

    async def get_snapshot_status(self, snapshot_id, *, source_worker_ip):
        self.status_worker_ips.append(source_worker_ip)
        request = next(request for request in self.create_requests if request.snapshot_id == snapshot_id)
        spec = {"backend_payload": {"value": request.snapshot_id, "nested": [None, "opaque"]}}
        return SnapshotOperationResult(
            snapshot_id, OperatorResultCode.ACCEPTED, "REMOTE_READY",
            {"snapshot_id": snapshot_id, "state": "REMOTE_READY", "progress": 1.0, "snapshot_spec": spec},
        )

    async def finalize_published_snapshot(self, snapshot_id, request, *, source_worker_ip):
        self.finalize_worker_ips.append(source_worker_ip)
        return SnapshotOperationResult(snapshot_id, OperatorResultCode.ACCEPTED, "REMOTE_READY")


def _manager(db_provider, operator: _BaseImageOperator) -> SnapshotManager:
    return SnapshotManager(SnapshotTable(db_provider), operator, MagicMock())


async def _set_state(db_provider, snapshot_id: str, state: str) -> None:
    def update() -> None:
        with db_provider.session_factory() as session, session.begin():
            row = session.get(RockSnapshotRecord, snapshot_id)
            row.state = state
            if state == "REMOTE_READY":
                row.spec = {"published": True}
                row.remote_ready_at = datetime.datetime.now(datetime.timezone.utc)
            if state == "DELETED":
                row.deleted_at = datetime.datetime.now(datetime.timezone.utc)

    await db_provider.run(update)


def test_base_image_build_key_matches_the_rust_protocol_golden():
    assert _canonical_base_image_build_key(
        "sha256:" + "a" * 64,
        "linux/amd64",
        "oci-ext4-v1-skopeo-e2fsprogs",
    ) == "sha256:a2a23dbea4ac5fcbf2539ec770e36022b4ddbfc417fe224cfbcdfd5e64bb23b9"


async def test_default_base_image_reuses_its_snapshot(db_provider):
    operator = _BaseImageOperator()
    operator.digests = {
        "registry.example/team/image:one": "sha256:" + "a" * 64,
        "registry.example/team/image:two": "sha256:" + "b" * 64,
    }
    manager = _manager(db_provider, operator)
    first = await manager.create_base_image(CreateBaseImageRequest(oci_ref="registry.example/team/image:one"))
    second = await manager.create_base_image(CreateBaseImageRequest(oci_ref="registry.example/team/image:two"))
    repeated_content = await manager.create_base_image(CreateBaseImageRequest(oci_ref="registry.example/team/image:one"))

    assert first["snapshot_id"] != second["snapshot_id"]
    assert repeated_content["snapshot_id"] == first["snapshot_id"]
    for record in (first, second):
        snapshot_id = record["snapshot_id"]
        assert snapshot_id.startswith("base-")
        parsed = uuid.UUID(hex=snapshot_id[5:])
        assert parsed.version == 4
        assert parsed.hex == snapshot_id[5:]


async def test_different_refs_with_one_digest_join_one_canonical_build(db_provider, monkeypatch):
    log_messages: list[str] = []
    monkeypatch.setattr("rock.admin.service.snapshot_manager.logger.info", log_messages.append)
    operator = _BaseImageOperator()
    digest = "sha256:" + "a" * 64
    operator.digests = {
        "registry.example/team/image:stable": digest,
        "registry.example/team/image:alias": digest,
    }
    manager = _manager(db_provider, operator)

    first = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-first", oci_ref="registry.example/team/image:stable"),
    )
    second = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-alias", oci_ref="registry.example/team/image:alias"),
    )

    assert first["snapshot_id"] == second["snapshot_id"] == "base-first"
    assert await SnapshotTable(db_provider).get("base-alias") is None
    assert operator.actual_builds == {"base-first"}
    assert {request.snapshot_id for request in operator.create_requests} == {"base-first"}
    # The canonical request is stable even when an alias ref joins while the
    # build is still CREATING, so snapd's per-snapshot single-flight dedupes it.
    assert {request.oci_ref for request in operator.create_requests} == {
        "registry.example/team/image:stable"
    }
    assert any(
        '"event":"admin_base_image_digest_claim"' in message
        and '"reused":true' in message
        and '"snapshot_id":"base-first"' in message
        for message in log_messages
    )


async def test_mutable_tag_new_digest_and_platform_or_builder_changes_do_not_reuse(db_provider):
    operator = _BaseImageOperator()
    ref = "registry.example/team/image:mutable"
    operator.digests[ref] = "sha256:" + "a" * 64
    manager = _manager(db_provider, operator)
    first = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-old", oci_ref=ref),
    )

    operator.digests[ref] = "sha256:" + "b" * 64
    moved = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-new", oci_ref=ref),
    )
    arm = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-arm", oci_ref=ref, platform="linux/arm64/v8"),
    )
    operator.builder_version = "test-oci-ext4-v2"
    rebuilt = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="base-v2", oci_ref=ref),
    )

    assert {first["snapshot_id"], moved["snapshot_id"], arm["snapshot_id"], rebuilt["snapshot_id"]} == {
        "base-old",
        "base-new",
        "base-arm",
        "base-v2",
    }


async def test_explicit_snapshot_id_is_an_artifact_not_a_permanent_request_alias(db_provider):
    operator = _BaseImageOperator()
    operator.digests = {
        "registry.example/image:a": "sha256:" + "a" * 64,
        "registry.example/image:b": "sha256:" + "b" * 64,
    }
    manager = _manager(db_provider, operator)
    request = CreateBaseImageRequest(snapshot_id="explicit-id", oci_ref="registry.example/image:a")

    first = await manager.create_base_image(request)
    replay = await manager.create_base_image(request)
    assert replay["snapshot_id"] == first["snapshot_id"]
    assert len(operator.resolve_requests) == 2

    operator.digests[request.oci_ref] = "sha256:" + "c" * 64
    with pytest.raises(SnapshotConflict):
        await manager.create_base_image(request)

    with pytest.raises(SnapshotConflict):
        await manager.create_base_image(
            CreateBaseImageRequest(snapshot_id="explicit-id", oci_ref="registry.example/image:b"),
        )
    with pytest.raises(SnapshotConflict):
        await manager.create_base_image(
            CreateBaseImageRequest(
                snapshot_id="explicit-id",
                oci_ref="registry.example/image:a",
                platform="linux/arm64/v8",
            ),
        )
    assert len(operator.resolve_requests) == 5


@pytest.mark.parametrize("corruption", ["platform", "build_key"])
async def test_invalid_snapd_resolution_is_rejected_before_catalog_claim(db_provider, corruption):
    operator = _BaseImageOperator()
    ref = "registry.example/image:stable"
    operator.digests[ref] = "sha256:" + "a" * 64
    original_resolve = operator.resolve_base_image

    async def corrupt_resolution(request):
        result = await original_resolve(request)
        payload = dict(result.payload)
        if corruption == "platform":
            payload["oci_platform"] = "linux/arm64/v8"
        else:
            payload["base_image_build_key"] = "sha256:" + "f" * 64
        return SnapshotOperationResult(
            result.snapshot_id,
            result.code,
            result.state,
            payload,
            result.last_error,
        )

    operator.resolve_base_image = corrupt_resolution
    manager = _manager(db_provider, operator)

    with pytest.raises(SnapshotConflict, match="another OCI platform|invalid BASE_IMAGE build key"):
        await manager.create_base_image(
            CreateBaseImageRequest(snapshot_id=f"invalid-{corruption}", oci_ref=ref),
        )
    assert await SnapshotTable(db_provider).get(f"invalid-{corruption}") is None
    assert operator.create_requests == []


async def test_remote_ready_is_reused_failed_retries_and_deleted_is_not_reused(db_provider):
    operator = _BaseImageOperator()
    digest = "sha256:" + "d" * 64
    operator.digests = {
        "registry.example/image:first": digest,
        "registry.example/image:ready-alias": digest,
        "registry.example/image:after-delete": digest,
    }
    manager = _manager(db_provider, operator)
    first = await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="lifecycle-first", oci_ref="registry.example/image:first"),
    )

    def set_state(state: str) -> None:
        with db_provider.session_factory() as session, session.begin():
            row = session.get(RockSnapshotRecord, first["snapshot_id"])
            row.state = state
            if state == "REMOTE_READY":
                row.spec = {"published": True}
                row.remote_ready_at = datetime.datetime.now(datetime.timezone.utc)
            if state == "DELETED":
                row.deleted_at = datetime.datetime.now(datetime.timezone.utc)

    await db_provider.run(set_state, "REMOTE_READY")
    calls_before_ready_alias = len(operator.create_requests)
    ready_alias = await manager.create_base_image(
        CreateBaseImageRequest(
            snapshot_id="lifecycle-ready-alias",
            oci_ref="registry.example/image:ready-alias",
        ),
    )
    assert ready_alias["snapshot_id"] == first["snapshot_id"]
    assert len(operator.create_requests) == calls_before_ready_alias

    await db_provider.run(set_state, "FAILED")
    await manager.create_base_image(
        CreateBaseImageRequest(snapshot_id="lifecycle-first", oci_ref="registry.example/image:first"),
    )
    assert len(operator.create_requests) == calls_before_ready_alias + 1

    await db_provider.run(set_state, "FAILED")
    operator.create_result_state = "FAILED"
    with pytest.raises(SnapshotConflict, match="terminal build failure"):
        await manager.create_base_image(
            CreateBaseImageRequest(
                snapshot_id="lifecycle-first",
                oci_ref="registry.example/image:first",
            ),
        )
    assert (await SnapshotTable(db_provider).get(first["snapshot_id"]))["state"] == "FAILED"

    await db_provider.run(set_state, "DELETED")
    operator.create_result_state = "CREATING"
    replacement = await manager.create_base_image(
        CreateBaseImageRequest(
            snapshot_id="lifecycle-replacement",
            oci_ref="registry.example/image:after-delete",
        ),
    )
    assert replacement["snapshot_id"] == "lifecycle-replacement"


async def test_joined_request_id_is_not_reserved_after_tag_changes(db_provider):
    operator = _BaseImageOperator()
    ref = "registry.example/image:mutable"
    operator.digests[ref] = "sha256:" + "a" * 64
    manager = _manager(db_provider, operator)
    first = await manager.create_base_image(CreateBaseImageRequest(snapshot_id="actual-base", oci_ref=ref))
    joined = await manager.create_base_image(CreateBaseImageRequest(snapshot_id="unused-request", oci_ref=ref))
    assert joined["snapshot_id"] == first["snapshot_id"]
    assert await SnapshotTable(db_provider).get("unused-request") is None

    operator.digests[ref] = "sha256:" + "b" * 64
    moved = await manager.create_base_image(CreateBaseImageRequest(snapshot_id="unused-request", oci_ref=ref))

    assert moved["snapshot_id"] == "unused-request"


@pytest.mark.parametrize("existing_state", ["CREATING", "FAILED"])
async def test_shared_base_image_keeps_claim_winner_owner_for_dispatch_and_reconciliation(db_provider, existing_state):
    operator = _BaseImageOperator()
    ref = "registry.example/image:shared"
    operator.digests[ref] = "sha256:" + "a" * 64
    manager = _manager(db_provider, operator)
    first = await manager.create_base_image(CreateBaseImageRequest(snapshot_id="winner-base", oci_ref=ref))
    await _set_state(db_provider, first["snapshot_id"], existing_state)

    operator.resolution_worker_ip = "10.20.30.99"
    joined = await manager.create_base_image(CreateBaseImageRequest(snapshot_id="joining-base", oci_ref=ref))

    assert joined["snapshot_id"] == first["snapshot_id"]
    assert joined["source_worker_ip"] == first["source_worker_ip"] == "10.20.30.40"
    assert operator.create_worker_ips == ["10.20.30.40", "10.20.30.40"]
    assert await SnapshotTable(db_provider).get("joining-base") is None

    assert await SnapshotReconciler(manager).reconcile_once() == 1
    assert operator.status_worker_ips == ["10.20.30.40"]
    assert operator.finalize_worker_ips == ["10.20.30.40"]


async def test_concurrent_managers_share_one_snapshot_build_identity(tmp_path, monkeypatch):
    def independent_connections(url, **kwargs):
        # The common in-memory fixture shares one sqlite connection across
        # threads. A file and independent connections are required to exercise
        # actual concurrent transactions safely, as in PostgreSQL.
        return create_engine(url, **{**kwargs, "poolclass": NullPool})

    monkeypatch.setattr("rock.admin.core.db_provider.create_engine", independent_connections)
    db_provider = DatabaseProvider(DatabaseConfig(url=f"sqlite:///{tmp_path / 'concurrent.db'}"))
    await db_provider.init()
    await db_provider.create_tables()
    operator = _BaseImageOperator()
    ref = "registry.example/image:concurrent"
    operator.digests[ref] = "sha256:" + "c" * 64
    managers = [_manager(db_provider, operator) for _ in range(8)]

    try:
        records = await asyncio.gather(*(
            manager.create_base_image(CreateBaseImageRequest(oci_ref=ref)) for manager in managers
        ))

        assert len({record["snapshot_id"] for record in records}) == 1
        assert len(operator.actual_builds) == 1
        assert len({request.model_dump_json() for request in operator.create_requests}) == 1
    finally:
        await db_provider.close()


async def test_unknown_dispatch_retains_snapshot_and_reuses_same_immutable_build(db_provider):
    operator = _BaseImageOperator()
    ref = "registry.example/image:unknown"
    operator.digests[ref] = "sha256:" + "a" * 64
    operator.lose_create_reply = True
    manager = _manager(db_provider, operator)

    with pytest.raises(SnapshotResultUnknown, match="lost create reply"):
        await manager.create_base_image(CreateBaseImageRequest(oci_ref=ref))

    first = operator.create_requests[0]
    assert (await SnapshotTable(db_provider).get(first.snapshot_id))["state"] == "CREATING"
    operator.lose_create_reply = False
    retried = await manager.create_base_image(CreateBaseImageRequest(oci_ref=ref))

    assert retried["snapshot_id"] == first.snapshot_id
    assert operator.create_requests[-1] == first


async def test_e2b_build_uses_its_allocated_snapshot_without_template_rows(db_provider):
    operator = _BaseImageOperator()
    ref = "registry.example/image:shared"
    operator.digests[ref] = "sha256:" + "a" * 64
    manager = _manager(db_provider, operator)
    adapter = E2BSnapshotService(manager, MagicMock())
    canonical = await manager.create_base_image(CreateBaseImageRequest(oci_ref=ref))
    identities = []
    for cpu in (2, 4):
        created = await adapter.create_template(E2BCreateTemplateRequest(cpuCount=cpu, name="example"))
        identities.append(created.template_id)
        await adapter.start_template_build(
            created.template_id, created.build_id, E2BStartTemplateBuildRequest(fromImage=ref),
        )
        record = await manager.get_snapshot_record(created.template_id)
        assert record["snapshot_id"] == created.template_id == created.build_id
        assert record["kind"] == "BASE_IMAGE"
        assert record["resource_spec"]["vcpus"] == cpu
        assert record["product_metadata"]["names"] == ["example"]
    assert len(set([canonical["snapshot_id"], *identities])) == 3
    await SnapshotReconciler(manager).reconcile_once()
    for snapshot_id in identities:
        assert (await adapter.get_template(snapshot_id)).builds[0].status == "ready"
    def count_templates():
        with db_provider.session_factory() as session:
            return session.query(TemplateRecord).count()
    assert await db_provider.run(count_templates) == 0


async def test_allocated_unbuilt_snapshot_is_not_dispatched_and_can_be_deleted(db_provider):
    manager = _manager(db_provider, _BaseImageOperator())
    adapter = E2BSnapshotService(manager, MagicMock())
    created = await adapter.create_template(E2BCreateTemplateRequest())
    assert await SnapshotReconciler(manager).reconcile_once() == 0
    manager._sandbox_lifecycle.list_active_snapshot_users = AsyncMock(return_value=[])
    await adapter.delete_template(created.template_id)
    assert (await manager._snapshots.get(created.template_id))["state"] == "DELETED"


async def test_native_and_prepare_share_snapshot_without_template_identity(db_provider):
    operator = _BaseImageOperator()
    ref = "registry.example/image:original"
    operator.digests[ref] = "sha256:" + "a" * 64
    manager = _manager(db_provider, operator)
    initial = await manager.create_base_image(CreateBaseImageRequest(oci_ref=ref))
    prepared = await manager.prepare_base_image(CreateBaseImageRequest(oci_ref=ref), timeout_seconds=1)
    assert prepared.snapshot_id == initial["snapshot_id"]
    assert "template_id" not in initial
    assert not hasattr(prepared, "template_id")
    assert len(operator.actual_builds) == 1
