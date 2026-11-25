"""Snapshot domain manager and peer collaboration with ``SandboxManager``."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import math
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rock.admin.core.snapshot_table import (
    BaseImageClaim,
    BaseImageResolutionIdentity,
    SnapshotIdentityConflict,
    SnapshotTable,
)
from rock.admin.proto.snapshot import (
    CreateBaseImageRequest,
    CreateSnapshotRequest,
    ForkSnapshotRequest,
    StartFromSnapshotRequest,
)
from rock.admin.service.snapshot_statemachine import InvalidSnapshotTransition, SnapshotStateMachine
from rock.admin.snapd.client import SnapdResultUnknown
from rock.admin.snapd.protocol import (
    MAX_FORK_CHILDREN,
    ChildLaunchPlan,
    FinalizePublishedSnapshotRequest,
    ForkSandboxRequest,
    ResolveBaseImageRequest,
    ResolvedBaseImageResult,
    SnapshotSandboxRequest,
    SnapshotStatusResult,
)
from rock.admin.snapd.protocol import CreateBaseImageRequest as SnapdCreateBaseImageRequest
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.snapd import OperatorResultCode, OperatorTarget, SnapshotOperationResult
from rock.sandbox.operator.snapshot import AbstractSnapshotOperator
from rock.sandbox.snapshot_types import (
    ForkChildLaunchPlan,
    ForkChildOutcome,
    ForkExecutionResult,
    ForkPreparation,
    LaunchableSnapshot,
    ManagerResultUnknown,
    SnapshotSource,
)

if TYPE_CHECKING:
    from rock.sandbox.sandbox_manager import SandboxManager

logger = init_logger(__name__)


def _log_start_stage(*, snapshot_id: str, sandbox_id: str, phase: str, started: float, total_started: float) -> None:
    logger.info(
        json.dumps(
            {
                "event": "admin_start_stage_completed",
                "component": "snapshot_manager",
                "snapshot_id": snapshot_id,
                "sandbox_id": sandbox_id,
                "phase": phase,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "total_duration_ms": round((time.perf_counter() - total_started) * 1000),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _log_base_image_claim(
    *,
    claim: BaseImageClaim,
    resolution_performed: bool,
) -> None:
    snapshot = claim.snapshot
    logger.info(
        json.dumps(
            {
                "event": "admin_base_image_digest_claim",
                "snapshot_id": snapshot["snapshot_id"],
                "base_image_build_key": snapshot.get("base_image_build_key"),
                "state": snapshot["state"],
                "reused": claim.reused,
                "dispatch": claim.dispatch,
                "resolution_performed": resolution_performed,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


class SnapshotNotFound(ValueError):
    pass


class SnapshotConflict(ValueError):
    pass


class SnapshotResultUnknown(ManagerResultUnknown):
    pass


def snapshot_spec_checksum(spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _full_snapshot_resource_spec(frozen: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    """Restore the captured VM geometry rather than applying cold-boot defaults."""
    try:
        vcpus = math.ceil(frozen.get("vcpus", frozen.get("cpus")))
        memory_mib = int(frozen["memory_mib"])
        if vcpus <= 0 or memory_mib <= 0:
            raise ValueError("CPU and memory must be positive")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise SnapshotConflict("FULL snapshot has no valid frozen CPU and memory specification") from error
    for key in ("vcpus", "cpus"):
        if key in requested and requested[key] != vcpus:
            raise SnapshotConflict("FULL snapshot CPU count cannot be changed")
    if "memory_mib" in requested and requested["memory_mib"] != memory_mib:
        raise SnapshotConflict("FULL snapshot memory size cannot be changed")
    resources = {**requested, **frozen, "vcpus": vcpus, "memory_mib": memory_mib}
    resources.pop("cpus", None)
    return resources


def _canonical_base_image_build_key(
    oci_digest: str,
    oci_platform: str,
    builder_version: str,
    *,
    format_version: int = 1,
) -> str:
    identity = {
        "base_image_builder_version": builder_version,
        "oci_digest": oci_digest,
        "oci_platform": oci_platform,
        "snapshot_format_version": format_version,
    }
    # ``canonical_json_bytes`` is also the immutable object encoder and adds a
    # single trailing newline.  This helper deliberately mirrors those exact
    # Rust bytes so Admin can independently reject a malformed snapd key.
    canonical = (
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class SnapshotManager:
    """Manage snapshots and delegate sandbox lifecycle to SandboxManager."""

    def __init__(
        self,
        snapshots: SnapshotTable,
        snapshot_operator: AbstractSnapshotOperator,
        sandbox_lifecycle: SandboxManager,
    ) -> None:
        self._snapshots = snapshots
        self.snapshot_operator = snapshot_operator
        self._sandbox_lifecycle = sandbox_lifecycle

    @staticmethod
    def _require_operation(result: SnapshotOperationResult, *, expected_state: str | None = None) -> dict[str, Any]:
        if result.code is OperatorResultCode.RESULT_UNKNOWN:
            raise SnapshotResultUnknown(result.last_error or "snapshot operation result is unknown")
        if result.code in {
            OperatorResultCode.FAILED,
            OperatorResultCode.IDENTITY_CONFLICT,
            OperatorResultCode.NOT_FOUND,
        }:
            raise SnapshotConflict(result.last_error or "snapshot operation failed")
        if expected_state is not None and result.state != expected_state:
            raise SnapshotConflict(f"snapshot operator did not establish {expected_state}")
        return result.payload

    async def create_base_image_draft(
        self, *, snapshot_id: str, resource_spec: dict[str, Any], product_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Allocate the snapshot identity used by an E2B two-stage build."""
        return await self._snapshots.create(
            snapshot_id=snapshot_id,
            kind="BASE_IMAGE",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            resource_spec=resource_spec,
            product_metadata=product_metadata,
        )

    async def create_base_image(self, request: CreateBaseImageRequest) -> dict[str, Any]:
        """Resolve OCI content and create or reuse a BASE_IMAGE snapshot."""
        resolution_operation = await self.snapshot_operator.resolve_base_image(
            ResolveBaseImageRequest(oci_ref=request.oci_ref, platform=request.platform)
        )
        resolution_payload = self._require_operation(resolution_operation)
        try:
            resolution = ResolvedBaseImageResult.model_validate(
                {key: value for key, value in resolution_payload.items() if key != "source_worker_ip"}
            )
        except ValueError as error:
            raise SnapshotConflict(f"snapd returned an invalid BASE_IMAGE resolution: {error}") from error
        if resolution.oci_ref != request.oci_ref:
            raise SnapshotConflict("snapd resolved another OCI reference")
        if resolution.oci_platform != request.platform:
            raise SnapshotConflict("snapd resolved another OCI platform")
        expected_build_key = _canonical_base_image_build_key(
            resolution.oci_digest,
            resolution.oci_platform,
            resolution.base_image_builder_version,
        )
        if resolution.base_image_build_key != expected_build_key:
            raise SnapshotConflict("snapd returned an invalid BASE_IMAGE build key")
        worker_ip = resolution_payload.get("source_worker_ip")
        if not isinstance(worker_ip, str):
            raise SnapshotConflict("BASE_IMAGE resolution did not return its source worker IP")
        try:
            claim = await self._snapshots.claim_base_image(
                snapshot_id=request.snapshot_id or f"base-{uuid.uuid4().hex}",
                resolution=BaseImageResolutionIdentity(
                    oci_ref=resolution.oci_ref,
                    oci_digest=resolution.oci_digest,
                    oci_platform=resolution.oci_platform,
                    base_image_builder_version=resolution.base_image_builder_version,
                    base_image_build_key=resolution.base_image_build_key,
                    source_worker_ip=worker_ip,
                ),
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
        except (SnapshotIdentityConflict, ValueError) as error:
            raise SnapshotConflict(str(error)) from error
        _log_base_image_claim(
            claim=claim,
            resolution_performed=True,
        )
        record = await self._dispatch_base_image_claim(claim)
        return record

    async def prepare_base_image(
        self, request: CreateBaseImageRequest, *, timeout_seconds: float,
    ) -> LaunchableSnapshot:
        """Import an ordinary OCI image through the same durable catalog API."""
        async def prepare() -> LaunchableSnapshot:
            record = await self.create_base_image(request)
            reconciler = SnapshotReconciler(self)
            while record["state"] != "REMOTE_READY":
                if record["state"] in {"FAILED", "DELETED"}:
                    raise SnapshotConflict(f"base image {record['snapshot_id']!r} is {record['state']}")
                # Refresh only this image; do not wait for a background scan of
                # the entire catalog or create a second import implementation.
                status = await reconciler._snapshot_status(record)
                await reconciler._converge_status(record, status)
                record = await self.get_snapshot_record(record["snapshot_id"])
                if record["state"] != "REMOTE_READY":
                    await asyncio.sleep(0.25)
            return await self.require_launchable_snapshot(record["snapshot_id"])

        return await asyncio.wait_for(prepare(), timeout=timeout_seconds)

    async def _dispatch_base_image_claim(self, claim: BaseImageClaim) -> dict[str, Any]:
        record = claim.snapshot
        if not claim.dispatch:
            return record
        result = await self.snapshot_operator.create_base_image(
            SnapdCreateBaseImageRequest(
                snapshot_id=record["snapshot_id"],
                oci_ref=record["oci_ref"],
                oci_digest=record["oci_digest"],
                platform=record["oci_platform"],
                base_image_builder_version=record["base_image_builder_version"],
                base_image_build_key=record["base_image_build_key"],
            ),
            source_worker_ip=record["source_worker_ip"],
        )
        try:
            self._require_operation(result)
        except SnapshotConflict:
            if result.state == "FAILED":
                await self._send_event_if_state(
                    record["snapshot_id"],
                    "fail",
                    {"CREATING"},
                )
            raise
        current = await self._snapshots.get(record["snapshot_id"])
        if current is None:
            raise SnapshotConflict("BASE_IMAGE catalog row disappeared after dispatch")
        return current

    async def require_launchable_snapshot(self, snapshot_id: str) -> LaunchableSnapshot:
        record = await self._require_snapshot(snapshot_id)
        spec = record.get("spec")
        if record["state"] != "REMOTE_READY" or not isinstance(spec, dict):
            raise SnapshotConflict(f"snapshot {snapshot_id!r} is not REMOTE_READY")
        return LaunchableSnapshot(
            snapshot_id=snapshot_id,
            kind=str(record["kind"]),
            format_version=int(record["format_version"]),
            spec=dict(spec),
            spec_checksum=snapshot_spec_checksum(spec),
            source_worker_ip=(
                record.get("source_worker_ip") if isinstance(record.get("source_worker_ip"), str) else None
            ),
            resource_spec=dict(record.get("resource_spec") or {}),
        )

    async def get_launchable_snapshot(self, snapshot_id: str) -> LaunchableSnapshot | None:
        try:
            return await self.require_launchable_snapshot(snapshot_id)
        except SnapshotNotFound:
            return None

    async def start_async(
        self,
        snapshot_id: str,
        request: StartFromSnapshotRequest,
        *,
        sandbox_namespace: str = "default",
        sandbox_image: str | None = None,
        deployment_config: DockerDeploymentConfig | None = None,
        sandbox_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        sandbox_id = request.sandbox_id or uuid.uuid4().hex
        request = request.model_copy(update={"sandbox_id": sandbox_id})
        catalog_started = time.perf_counter()
        snapshot = await self.require_launchable_snapshot(snapshot_id)
        if snapshot.kind == "FULL":
            resources = _full_snapshot_resource_spec(snapshot.resource_spec, request.resource_spec)
            request = request.model_copy(update={"resource_spec": resources})
            if deployment_config is not None:
                deployment_config = deployment_config.model_copy(update={
                    "cpus": resources["vcpus"],
                    "memory": f"{resources['memory_mib']}M",
                    "disk": resources.get("disk"),
                })
        _log_start_stage(
            snapshot_id=snapshot_id,
            sandbox_id=sandbox_id,
            phase="catalog_read",
            started=catalog_started,
            total_started=total_started,
        )
        lifecycle_started = time.perf_counter()
        result = await self._sandbox_lifecycle.start_from_snapshot(
            snapshot,
            request,
            sandbox_namespace=sandbox_namespace,
            sandbox_image=sandbox_image,
            **({"deployment_config": deployment_config, "sandbox_metadata": sandbox_metadata}
               if deployment_config is not None else {}),
        )
        _log_start_stage(
            snapshot_id=snapshot_id,
            sandbox_id=sandbox_id,
            phase="sandbox_lifecycle",
            started=lifecycle_started,
            total_started=total_started,
        )
        return result

    async def capture_snapshot(
        self,
        source: SnapshotSource,
        request: CreateSnapshotRequest,
    ) -> dict[str, Any]:
        snapshot_id = request.snapshot_id or f"snap-{uuid.uuid4().hex}"
        source = await self._prepare_cut_attempt(source, snapshot_id=snapshot_id)
        try:
            operation = await self.snapshot_operator.capture_snapshot(
                source,
                SnapshotSandboxRequest(
                    snapshot_id=snapshot_id,

                ),
            )
            self._require_operation(operation, expected_state="LOCAL_READY")
        except SnapdResultUnknown as error:
            raise SnapshotResultUnknown(str(error)) from error
        except SnapshotResultUnknown:
            raise
        except SnapshotConflict:
            await self._send_event_if_state(snapshot_id, "fail", {"CREATING"})
            raise
        transitioned = await self._send_event_if_state(snapshot_id, "local_ready", {"CREATING"})
        return transitioned or await self._require_snapshot(snapshot_id)

    async def prepare_fork(
        self,
        source: SnapshotSource,
        request: ForkSnapshotRequest,
        *,
        child_timeout_seconds: int | None,
    ) -> ForkPreparation:
        total_started = time.perf_counter()
        if child_timeout_seconds is not None and not 1 <= child_timeout_seconds <= 86_400:
            raise ValueError("child timeout must be in 1..86400 seconds")
        if request.count > MAX_FORK_CHILDREN:
            raise ValueError(f"one fork may create at most {MAX_FORK_CHILDREN} children")
        snapshot_id = request.snapshot_id or f"snap-{uuid.uuid4().hex}"
        resource_spec = _full_snapshot_resource_spec(source.resource_spec, request.resource_spec)
        attempt_started = time.perf_counter()
        target_ids = tuple(request.target_sandbox_ids) if request.target_sandbox_ids is not None else tuple(
            uuid.uuid4().hex for _ in range(request.count)
        )
        if request.target_sandbox_ids is not None and (
            len(target_ids) != request.count or len(set(target_ids)) != len(target_ids)
        ):
            raise SnapshotConflict("fork target sandbox identities must be unique and match count")
        for sandbox_id in target_ids:
            _validate_sandbox_id(sandbox_id)
        source = await self._prepare_cut_attempt(source, snapshot_id=snapshot_id)
        _log_start_stage(
            snapshot_id=snapshot_id,
            sandbox_id=f"fork:{len(target_ids)}",
            phase="fork_cut_attempt",
            started=attempt_started,
            total_started=total_started,
        )
        return ForkPreparation(
            source=source,
            snapshot_id=snapshot_id,
            sandbox_namespace=source.sandbox_namespace,

            target_sandbox_ids=target_ids,
            resource_spec=resource_spec,
            child_timeout_seconds=child_timeout_seconds,
        )

    async def execute_fork(
        self,
        preparation: ForkPreparation,
        children: tuple[ForkChildLaunchPlan, ...],
    ) -> ForkExecutionResult:
        total_started = time.perf_counter()
        if tuple(child.sandbox_id for child in children) != preparation.target_sandbox_ids:
            raise SnapshotConflict("fork child plan differs from the prepared request")
        operator_started = time.perf_counter()
        try:
            operation = await self.snapshot_operator.fork_snapshot(
                preparation.source,
                ForkSandboxRequest(
                    snapshot_id=preparation.snapshot_id,

                    children=[
                        ChildLaunchPlan(
                            sandbox_id=child.sandbox_id,

                            resource_spec=child.resource_spec,
                        )
                        for child in children
                    ],
                ),
            )
        except SnapdResultUnknown as error:
            raise SnapshotResultUnknown(str(error)) from error
        _log_start_stage(
            snapshot_id=preparation.snapshot_id,
            sandbox_id=f"fork:{len(children)}",
            phase="fork_operator",
            started=operator_started,
            total_started=total_started,
        )
        raw_children = tuple(operation.payload.get("children", ()))
        if operation.code is OperatorResultCode.RESULT_UNKNOWN:
            by_id = {str(item.get("sandbox_id")): item for item in raw_children}
            if len(by_id) != len(raw_children) or not by_id.keys() <= set(preparation.target_sandbox_ids):
                raise SnapshotResultUnknown("snapshot operator returned an invalid unknown fork child set")
            raw_children = tuple(
                {
                    "sandbox_id": sandbox_id,
                    "state": "PENDING",
                    "last_error": operation.last_error,
                    **by_id.get(sandbox_id, {}),
                }
                for sandbox_id in preparation.target_sandbox_ids
            )
        elif not raw_children and operation.code in {
            OperatorResultCode.FAILED,
            OperatorResultCode.IDENTITY_CONFLICT,
        }:
            raw_children = tuple(
                {
                    "sandbox_id": sandbox_id,
                    "state": "FAILED",
                    "last_error": operation.last_error,
                }
                for sandbox_id in preparation.target_sandbox_ids
            )
        if tuple(str(item.get("sandbox_id")) for item in raw_children) != preparation.target_sandbox_ids:
            raise SnapshotConflict("snapshot operator returned an invalid fork child set or order")
        outcomes = tuple(
            ForkChildOutcome(
                sandbox_id=str(item["sandbox_id"]),
                state=str(item.get("state", "FAILED")).upper(),
                target=_target_from_payload(item.get("placement")),
                last_error=item.get("last_error") if isinstance(item.get("last_error"), str) else None,
                guest_endpoint=(dict(item["guest_endpoint"]) if isinstance(item.get("guest_endpoint"), dict) else None),
                vmm_pid=item.get("vmm_pid") if isinstance(item.get("vmm_pid"), int) else None,
                rocklet_artifact=(
                    dict(item["rocklet_artifact"]) if isinstance(item.get("rocklet_artifact"), dict) else None
                ),
            )
            for item in raw_children
        )
        return ForkExecutionResult(
            code=operation.code,
            state=operation.state,
            children=outcomes,
            last_error=operation.last_error,
        )

    async def complete_fork(
        self,
        preparation: ForkPreparation,
        result: ForkExecutionResult,
    ) -> dict[str, Any]:
        failed = result.code in {OperatorResultCode.FAILED, OperatorResultCode.IDENTITY_CONFLICT}
        event = "fail" if failed else "local_ready"
        transitioned = await self._send_event_if_state(
            preparation.snapshot_id,
            event,
            {"CREATING"},
        )
        record = transitioned or await self._require_snapshot(preparation.snapshot_id)
        return record

    async def get_snapshot_record(self, snapshot_id: str) -> dict[str, Any]:
        record = await self._require_snapshot(snapshot_id)
        if record["state"] == "DELETED":
            raise SnapshotNotFound(f"snapshot {snapshot_id!r} does not exist")
        return record

    async def wait_remote_ready(self, snapshot_id: str, *, timeout_seconds: float = 300) -> dict[str, Any]:
        """Observe and finalize a checkpoint before its source runtime is stopped."""
        reconciler = SnapshotReconciler(self)
        deadline = time.monotonic() + timeout_seconds
        while True:
            row = await self.get_snapshot_record(snapshot_id)
            if row["state"] == "FAILED":
                raise SnapshotConflict(f"pause checkpoint {snapshot_id} failed")
            if row["state"] == "REMOTE_READY":
                await reconciler._finalize_published(row, row["spec"])
                return row
            status = await reconciler._snapshot_status(row)
            await reconciler._converge_status(row, status)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"pause checkpoint {snapshot_id} is not REMOTE_READY after {timeout_seconds}s")
            await asyncio.sleep(0.25)

    async def list_snapshot_records(
        self,
        *,
        source_sandbox_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._snapshots.list(
            source_sandbox_id=source_sandbox_id,
            offset=offset,
            limit=limit,
        )

    async def _prepare_cut_attempt(
        self,
        source: SnapshotSource,
        *,
        snapshot_id: str,
    ) -> SnapshotSource:
        if await self._snapshots.get(snapshot_id) is not None:
            raise SnapshotConflict(f"snapshot {snapshot_id!r} already exists")
        try:
            source = await self.snapshot_operator.resolve_source(source)
        except SnapdResultUnknown as error:
            raise SnapshotResultUnknown(str(error)) from error
        source = replace(source, resource_spec=_full_snapshot_resource_spec(source.resource_spec, {}))
        try:
            await self._snapshots.create_full_snapshot_attempt(
                snapshot_id=snapshot_id,
                source_sandbox_id=source.sandbox_id,
                source_worker_ip=source.target.host_ip or "",
                resource_spec=source.resource_spec,
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
            return source
        except SnapshotIdentityConflict as error:
            # A primary-key race must not dispatch or fail the winning request.
            raise SnapshotConflict(str(error)) from error

    async def delete_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Soft-delete the catalog entry after checking durable sandbox users."""
        snapshot = await self._require_snapshot(snapshot_id)
        if snapshot["state"] == "DELETED":
            return snapshot
        # This usage check is not atomic with sandbox admission. An in-flight
        # start may finish after soft deletion; storage reclamation is separate.
        try:
            active_users = await self._sandbox_lifecycle.list_active_snapshot_users(
                snapshot_id,
            )
        except ValueError as error:
            raise SnapshotConflict("snapshot runtime usage is indeterminate") from error
        if active_users:
            identities = ", ".join(user.sandbox_id for user in active_users)
            raise SnapshotConflict(f"snapshot is referenced by active sandbox runtimes: {identities}")
        if snapshot["kind"] == "FULL" and snapshot["state"] in {"CREATING", "LOCAL_READY"}:
            source = await self._sandbox_lifecycle.get_product_view(snapshot["source_sandbox_id"])
            if source is not None and source.state not in {"stopped", "archived", "deleted"}:
                raise SnapshotConflict("end the source sandbox before deleting an unfinished FULL snapshot")
            status = await self.snapshot_operator.get_snapshot_status(
                snapshot_id,
                source_worker_ip=snapshot["source_worker_ip"],
            )
            if status.code is not OperatorResultCode.NOT_FOUND:
                self._require_operation(status)
                raise SnapshotConflict("snapshot is still present on its source worker")
            await self._send_event_if_state(snapshot_id, "fail", {"CREATING", "LOCAL_READY"})
        if snapshot["kind"] == "BASE_IMAGE" and not snapshot.get("oci_digest") and snapshot["state"] == "CREATING":
            await self._send_event_if_state(snapshot_id, "fail", {"CREATING"})
        return await self._send_snapshot_event(snapshot_id, "delete")

    async def _require_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = await self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise SnapshotNotFound(f"snapshot {snapshot_id!r} does not exist")
        return snapshot

    async def _send_snapshot_event(
        self,
        snapshot_id: str,
        event: str,
        **payload: Any,
    ) -> dict[str, Any]:
        snapshot = await self._require_snapshot(snapshot_id)
        machine = await SnapshotStateMachine.from_record(snapshot, self._snapshots)
        try:
            return await machine.send(event, **payload)
        except (InvalidSnapshotTransition, SnapshotIdentityConflict) as error:
            raise SnapshotConflict(str(error)) from error

    async def _send_event_if_state(
        self,
        snapshot_id: str,
        event: str,
        source_states: set[str],
        **payload: Any,
    ) -> dict[str, Any] | None:
        snapshot = await self._require_snapshot(snapshot_id)
        if snapshot["state"] not in source_states:
            return None
        machine = await SnapshotStateMachine.from_record(snapshot, self._snapshots)
        try:
            return await machine.send(event, **payload)
        except (InvalidSnapshotTransition, SnapshotIdentityConflict) as error:
            raise SnapshotConflict(str(error)) from error


class SnapshotReconciler:
    """Converge durable catalog rows from the snapshot operator observation."""

    def __init__(self, manager: SnapshotManager) -> None:
        self._manager = manager
        self._finalized_checksums: dict[str, str] = {}
        self._scan_cursors: dict[str, tuple[datetime.datetime, str]] = {}

    async def _next_batch(self, state: str, limit: int) -> list[dict[str, Any]]:
        cursor = self._scan_cursors.get(state)
        rows = await self._manager._snapshots.list_by_state(state, limit=limit, after=cursor)
        if not rows and cursor is not None:
            self._scan_cursors.pop(state, None)
            rows = await self._manager._snapshots.list_by_state(state, limit=limit)
        return rows

    async def _finalize_published(self, row: dict[str, Any], spec: dict[str, Any]) -> None:
        snapshot_id = row["snapshot_id"]
        checksum = snapshot_spec_checksum(spec)
        if self._finalized_checksums.get(snapshot_id) == checksum:
            return
        result = await self._manager.snapshot_operator.finalize_published_snapshot(
            snapshot_id,
            FinalizePublishedSnapshotRequest(
                spec_checksum=checksum,
            ),
            source_worker_ip=row.get("source_worker_ip") or "",
        )
        self._manager._require_operation(result)
        self._finalized_checksums[snapshot_id] = checksum

    async def reconcile_once(self, *, limit: int = 100) -> int:
        reconciled = 0
        for state in ("CREATING", "LOCAL_READY"):
            rows = await self._next_batch(state, limit)
            for row in rows:
                # Advance only over attempted rows, including failed attempts.
                # An unexpected error must not skip the rest of this page.
                self._scan_cursors[state] = (row["created_at"], row["snapshot_id"])
                if row["kind"] == "BASE_IMAGE" and row.get("product_metadata") is not None and not row.get("oci_digest"):
                    # Allocated by E2B; no worker job exists until build submission.
                    continue
                try:
                    status = await self._snapshot_status(row)
                    if await self._converge_status(row, status):
                        reconciled += 1
                except (SnapshotResultUnknown, SnapshotConflict):
                    continue
        remote_rows = await self._next_batch("REMOTE_READY", limit)
        for row in remote_rows:
            self._scan_cursors["REMOTE_READY"] = (row["created_at"], row["snapshot_id"])
            spec = row.get("spec")
            if not isinstance(spec, dict):
                continue
            try:
                await self._finalize_published(row, spec)
            except (SnapshotResultUnknown, SnapshotConflict):
                continue
        return reconciled

    async def _snapshot_status(self, row: dict[str, Any]) -> SnapshotStatusResult:
        result = await self._manager.snapshot_operator.get_snapshot_status(
            row["snapshot_id"],
            source_worker_ip=row.get("source_worker_ip") or "",
        )
        status = SnapshotStatusResult.model_validate(self._manager._require_operation(result))
        if status.snapshot_id != row["snapshot_id"]:
            raise SnapshotConflict("snapshot operator returned another snapshot identity")
        return status

    async def _converge_status(self, row: dict[str, Any], status: SnapshotStatusResult) -> bool:
        snapshot_id = row["snapshot_id"]
        catalog_state = row["state"]
        if status.state == "FAILED":
            failed = await self._manager._send_event_if_state(snapshot_id, "fail", {"CREATING", "LOCAL_READY"})
            return failed is not None
        if status.state == "CREATING":
            return False
        changed = False
        if catalog_state == "CREATING" and status.state in {"LOCAL_READY", "REMOTE_READY"}:
            transitioned = await self._manager._send_event_if_state(snapshot_id, "local_ready", {"CREATING"})
            current = transitioned or await self._manager._snapshots.get(snapshot_id)
            if current is None or current["state"] not in {"LOCAL_READY", "REMOTE_READY"}:
                return False
            catalog_state = current["state"]
            changed = transitioned is not None
        if status.state != "REMOTE_READY":
            return changed
        if status.snapshot_spec is None:
            raise SnapshotConflict("snapshot operator REMOTE_READY status has no immutable spec")
        spec = status.snapshot_spec
        if catalog_state == "LOCAL_READY":
            committed = await self._manager._send_event_if_state(
                snapshot_id, "remote_ready", {"LOCAL_READY"}, spec=spec,
                memory_bytes=status.memory_bytes, rootfs_bytes=status.rootfs_bytes,
            )
            if committed is None:
                current = await self._manager._snapshots.get(snapshot_id)
                if current is None or current["state"] != "REMOTE_READY" or current["spec"] != spec:
                    return changed
            else:
                changed = True
        current_row = await self._manager._snapshots.get(snapshot_id)
        assert current_row is not None
        await self._finalize_published(current_row, spec)
        return changed


def _target_from_payload(value: object) -> OperatorTarget:
    placement = value if isinstance(value, dict) else {}
    return OperatorTarget(
        node_id=placement.get("node_id") if isinstance(placement.get("node_id"), str) else None,
        host_ip=placement.get("host_ip") if isinstance(placement.get("host_ip"), str) else None,
        host_name=placement.get("host_name") if isinstance(placement.get("host_name"), str) else None,
    )


def _validate_sandbox_id(sandbox_id: str) -> None:
    if not isinstance(sandbox_id, str) or not 1 <= len(sandbox_id.encode()) <= 128:
        raise ValueError("sandbox identity must contain 1..128 UTF-8 bytes")
    if any(ord(char) < 0x21 or char in "/?#" for char in sandbox_id):
        raise ValueError("sandbox identity is not path-segment safe")
