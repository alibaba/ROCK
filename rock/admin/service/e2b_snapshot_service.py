"""E2B wire adapter over the canonical snapshot catalog.

This module intentionally projects only product metadata.  Canonical snapshot
specs, object locations and storage credential references never cross the E2B
API boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime
import uuid
from typing import Any

from rock.admin.proto.e2b_snapshot import (
    E2BCreateSnapshotRequest,
    E2BCreateTemplateRequest,
    E2BCreateTemplateResponse,
    E2BForkError,
    E2BForkRequest,
    E2BForkResult,
    E2BSnapshot,
    E2BStartTemplateBuildRequest,
    E2BTemplate,
    E2BTemplateBuild,
)
from rock.admin.proto.response import E2BCreateSandboxResponse
from rock.admin.proto.snapshot import CreateBaseImageRequest, CreateSnapshotRequest, ForkSnapshotRequest
from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotNotFound
from rock.common.constants import E2B_CLIENT_ID, E2B_ENVD_VERSION

_DEFAULT_CPU_COUNT = 2
_DEFAULT_MEMORY_MB = 8192
_DEFAULT_DISK_SIZE_MB = 50 * 1024
_STATUS = {
    "CREATING": "building",
    "LOCAL_READY": "building",
    "REMOTE_READY": "ready",
    "FAILED": "error",
}


class E2BSnapshotService:
    def __init__(
        self, snapshot_manager, sandbox_manager, *, ready_timeout: float = 30.0
    ) -> None:
        self._snapshot_manager = snapshot_manager
        self._sandbox_manager = sandbox_manager
        self._ready_timeout = ready_timeout

    async def create_snapshot(
        self,
        source_id: str,
        request: E2BCreateSnapshotRequest,
    ) -> E2BSnapshot:
        record = await self._sandbox_manager.snapshot_sandbox(
            source_id,
            CreateSnapshotRequest(),
        )
        record = await self._wait_remote_ready(record["snapshot_id"])
        return self._snapshot_view(record)

    async def fork_sandbox(
        self,
        source_id: str,
        request: E2BForkRequest,
    ) -> list[E2BForkResult]:
        snapshot, children = await self._sandbox_manager.fork_sandbox(
            source_id,
            ForkSnapshotRequest(count=request.count),
            child_timeout_seconds=request.timeout,
        )
        results: list[E2BForkResult] = []
        for child in children:
            sandbox_id = child.get("sandbox_id", "")
            if child.get("state") != "RUNNING":
                pending = child.get("state") == "PENDING"
                message = child.get("last_error") or ("fork child result is unknown" if pending else "fork child failed")
                results.append(E2BForkResult(error=E2BForkError(code=503 if pending else 500, message=message)))
                continue
            row = await self._sandbox_manager.get_product_view(sandbox_id)
            token = row.auth_token if row else None
            results.append(
                E2BForkResult(
                    sandbox=E2BCreateSandboxResponse(
                        sandboxID=sandbox_id,
                        envdVersion=E2B_ENVD_VERSION,
                        clientID=E2B_CLIENT_ID,
                        templateID=snapshot["snapshot_id"],
                        envdAccessToken=token if isinstance(token, str) and token else None,
                    )
                )
            )
        return results

    async def create_template(self, request: E2BCreateTemplateRequest) -> E2BCreateTemplateResponse:
        template_id = f"base-{uuid.uuid4().hex}"
        names = [request.name] if request.name else []
        aliases = [request.alias] if request.alias else []
        await self._snapshot_manager.create_base_image_draft(
            snapshot_id=template_id,
            resource_spec={"vcpus": request.cpu_count, "memory_mib": request.memory_mb,
                           "disk_size_mb": _DEFAULT_DISK_SIZE_MB},
            product_metadata={"names": names, "tags": request.tags, "aliases": aliases, "teamID": request.team_id},
        )
        return E2BCreateTemplateResponse(
            templateID=template_id,
            buildID=template_id,
            names=names,
            tags=request.tags,
            aliases=aliases,
        )

    async def start_template_build(
        self,
        template_id: str,
        build_id: str,
        request: E2BStartTemplateBuildRequest,
    ) -> None:
        if template_id != build_id:
            raise SnapshotConflict("ROCK template and build identity must match")
        record = await self._require_template(template_id)
        if record.get("product_metadata") is None:
            raise SnapshotConflict("build requires an allocated BASE_IMAGE snapshot")
        await self._snapshot_manager.create_base_image(
            CreateBaseImageRequest(snapshot_id=template_id, oci_ref=request.from_image),
        )

    async def get_snapshot(self, snapshot_id: str) -> E2BSnapshot:
        record = await self._require_snapshot(snapshot_id)
        return self._snapshot_view(record)

    async def list_snapshots(
        self,
        *,
        limit: int = 100,
        next_token: str | None = None,
    ) -> tuple[list[E2BSnapshot], str | None]:
        offset = _decode_cursor(next_token)
        records = await self._snapshot_manager.list_snapshot_records(
            offset=offset,
            limit=limit + 1,
        )
        has_more = len(records) > limit
        page = records[:limit]
        token = _encode_cursor(offset + limit) if has_more else None
        return [self._snapshot_view(record) for record in page], token

    async def get_template(self, template_id: str) -> E2BTemplate:
        record = await self._require_template(template_id)
        policy = record.get("resource_spec") or {}
        created_at = _aware_datetime(record["created_at"])
        remote_ready_at = record.get("remote_ready_at")
        updated_at = _aware_datetime(remote_ready_at) if remote_ready_at is not None else created_at
        status = _STATUS[record["state"]]
        finished_at = updated_at if status in {"ready", "error"} else None
        metadata = record.get("product_metadata") or {}
        return E2BTemplate(
            templateID=template_id,
            names=metadata.get("names", []),
            aliases=metadata.get("aliases", []),
            createdAt=created_at,
            updatedAt=updated_at,
            builds=[
                E2BTemplateBuild(
                    # ROCK intentionally has no second mutable build identity.
                    buildID=template_id,
                    status=status,
                    createdAt=created_at,
                    updatedAt=updated_at,
                    finishedAt=finished_at,
                    cpuCount=policy.get("vcpus", _DEFAULT_CPU_COUNT),
                    memoryMB=policy.get("memory_mib", _DEFAULT_MEMORY_MB),
                    diskSizeMB=policy.get("disk_size_mb", _DEFAULT_DISK_SIZE_MB),
                    envdVersion=E2B_ENVD_VERSION,
                )
            ],
        )

    async def delete_template(self, template_id: str) -> None:
        await self._require_template(template_id)
        await self._snapshot_manager.delete_snapshot(template_id)

    async def get_start_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        record = await self._require_snapshot(snapshot_id)
        if record["state"] != "REMOTE_READY":
            raise SnapshotConflict(f"snapshot {snapshot_id!r} is not REMOTE_READY")
        return record

    async def _require_template(self, template_id: str) -> dict[str, Any]:
        try:
            record = await self._require_snapshot(template_id)
        except SnapshotNotFound:
            raise SnapshotNotFound(f"template {template_id!r} does not exist") from None
        if record["kind"] != "BASE_IMAGE":
            raise SnapshotNotFound(f"template {template_id!r} does not exist")
        return record

    async def _wait_remote_ready(self, snapshot_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while True:
            record = await self._require_snapshot(snapshot_id)
            if record["state"] == "REMOTE_READY":
                return record
            if record["state"] == "FAILED":
                raise SnapshotConflict("snapshot publication failed")
            if asyncio.get_running_loop().time() >= deadline:
                raise E2BSnapshotTimeout("snapshot did not reach REMOTE_READY before the E2B deadline")
            await asyncio.sleep(0.05)

    async def _require_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        return await self._snapshot_manager.get_snapshot_record(snapshot_id)

    @staticmethod
    def _snapshot_view(record: dict[str, Any]) -> E2BSnapshot:
        return E2BSnapshot(snapshotID=record["snapshot_id"], names=[])


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"rock-e2b-v1:{offset}".encode()).decode().rstrip("=")


def _decode_cursor(token: str | None) -> int:
    if token is None:
        return 0
    try:
        padded = token + "=" * (-len(token) % 4)
        value = base64.urlsafe_b64decode(padded.encode()).decode()
        prefix, raw_offset = value.split(":", 1)
        offset = int(raw_offset)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("nextToken is invalid") from None
    if prefix != "rock-e2b-v1" or offset < 0:
        raise ValueError("nextToken is invalid")
    return offset


def _aware_datetime(value: object) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise ValueError("snapshot timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


class E2BSnapshotTimeout(TimeoutError):
    pass
