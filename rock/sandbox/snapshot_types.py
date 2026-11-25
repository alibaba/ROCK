"""Snapshot data types shared by sandbox and snapshot managers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rock.sandbox.operator.snapd import OperatorResultCode, OperatorTarget


class ManagerResultUnknown(RuntimeError):
    """A durable operation may have committed but its observation was lost."""


@dataclass(frozen=True, slots=True)
class LaunchableSnapshot:
    snapshot_id: str
    kind: str
    format_version: int
    spec: dict[str, Any]
    spec_checksum: str
    source_worker_ip: str | None = None
    resource_spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActiveSnapshotUser:
    """Durable sandbox runtime that still protects snapshot backing."""

    sandbox_id: str
    source_snapshot_id: str
    lifecycle_state: str
    placement: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    sandbox_id: str
    sandbox_namespace: str

    target: OperatorTarget
    resource_spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ForkPreparation:
    source: SnapshotSource
    snapshot_id: str
    sandbox_namespace: str

    target_sandbox_ids: tuple[str, ...]
    resource_spec: dict[str, Any]
    child_timeout_seconds: int | None


@dataclass(frozen=True, slots=True)
class ForkChildLaunchPlan:
    sandbox_id: str

    resource_spec: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ForkChildOutcome:
    sandbox_id: str
    state: str
    target: OperatorTarget = field(default_factory=OperatorTarget)
    last_error: str | None = None
    guest_endpoint: dict[str, Any] | None = None
    vmm_pid: int | None = None
    rocklet_artifact: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ForkExecutionResult:
    code: OperatorResultCode
    state: str
    children: tuple[ForkChildOutcome, ...]
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxProductView:
    sandbox_id: str
    sandbox_namespace: str
    state: str
    source_snapshot_id: str | None
    image: str
    auth_token: str | None
