"""Versioned JSON contract used between ROCK-admin and rock-snapd.

Only metadata crosses this boundary. File descriptors, local paths, credentials,
and memory/block payloads belong to the node-local snapd/VMM boundary.
"""

from __future__ import annotations

import ipaddress
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SNAPD_PROTOCOL_VERSION = 1
MAX_FORK_CHILDREN = 32


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapdErrorCode(str, Enum):
    OK = "OK"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    CONTENT_IDENTITY_CONFLICT = "CONTENT_IDENTITY_CONFLICT"
    INVALID_STATE = "INVALID_STATE"
    SNAPSHOT_IN_USE = "SNAPSHOT_IN_USE"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    INTERNAL = "INTERNAL"


class SnapdReply(StrictModel):
    request_id: str = Field(min_length=1)
    code: SnapdErrorCode
    retryable: bool = False
    error_message: str | None = None
    result: dict[str, Any] | None = None

    @field_validator("error_message")
    @classmethod
    def success_has_no_error_message(cls, value: str | None, info):
        if info.data.get("code") == SnapdErrorCode.OK and value is not None:
            raise ValueError("successful reply must not include error_message")
        return value


class CreateBaseImageRequest(StrictModel):
    snapshot_id: str = Field(min_length=1)
    oci_ref: str = Field(min_length=1)
    oci_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: str = "linux/amd64"
    base_image_builder_version: str = Field(min_length=1, max_length=256)
    base_image_build_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResolveBaseImageRequest(StrictModel):
    oci_ref: str = Field(min_length=1)
    platform: str = "linux/amd64"


class ResolvedBaseImageResult(StrictModel):
    oci_ref: str = Field(min_length=1)
    oci_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    oci_platform: str = Field(min_length=1, max_length=256)
    base_image_builder_version: str = Field(min_length=1, max_length=256)
    base_image_build_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SnapshotSandboxRequest(StrictModel):
    snapshot_id: str = Field(min_length=1)


class ChildLaunchPlan(StrictModel):
    sandbox_id: str = Field(min_length=1)

    resource_spec: dict[str, Any] = Field(default_factory=dict)


class ForkSandboxRequest(SnapshotSandboxRequest):
    children: list[ChildLaunchPlan] = Field(min_length=1, max_length=MAX_FORK_CHILDREN)

    @field_validator("children")
    @classmethod
    def children_are_unique(cls, children: list[ChildLaunchPlan]):
        sandbox_ids = [child.sandbox_id for child in children]
        if len(set(sandbox_ids)) != len(sandbox_ids):
            raise ValueError("fork child sandbox IDs must be unique")
        return children


class StartSandboxFromSnapshotRequest(StrictModel):
    sandbox_id: str = Field(min_length=1)

    snapshot_spec: dict[str, Any]
    spec_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resource_spec: dict[str, Any] = Field(default_factory=dict)
    p2p_source_worker_ip: str | None = None

    @field_validator("p2p_source_worker_ip")
    @classmethod
    def p2p_source_is_ip_literal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as error:
            raise ValueError("p2p_source_worker_ip must be an IP literal") from error


class LayerUploadStatus(StrictModel):
    upload_state: Literal["LOCAL_READY", "REMOTE_READY"]
    remote_publishing: bool = False
    attempt_count: int = Field(default=0, ge=0)
    last_error: str | None = None
    retry_at_unix_ms: int | None = Field(default=None, ge=0)


class SnapshotStatusResult(StrictModel):
    snapshot_id: str
    state: Literal["CREATING", "LOCAL_READY", "REMOTE_READY", "FAILED"]
    remote_publishing: bool = False
    progress: float = Field(ge=0.0, le=1.0)
    attempt_count: int = Field(default=0, ge=0)
    last_error: str | None = None
    failure_code: str | None = None
    retryable: bool = False
    retry_at_unix_ms: int | None = Field(default=None, ge=0)
    snapshot_spec: dict[str, Any] | None = None
    memory_bytes: int | None = Field(default=None, strict=True, gt=0)
    rootfs_bytes: int | None = Field(default=None, strict=True, gt=0)
    spec_checksum: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    admin_finalized: bool = False
    catalog_pin_held: bool = False
    layers: dict[Literal["MEMORY", "BLOCK"], LayerUploadStatus] = Field(default_factory=dict)


class SnapdGuestEndpoint(StrictModel):
    """Worker-local forwarder details returned by snapd without an address."""

    transport: Literal["vsock-tcp-forward"]
    host_port: int = Field(strict=True, ge=1, le=65_535)
    guest_port: int = Field(strict=True, ge=1, le=4_294_967_295)
    vsock_cid: int = Field(strict=True, ge=3, le=4_294_967_295)


class GuestEndpoint(SnapdGuestEndpoint):
    """Admin endpoint composed from Ray placement and snapd's proxy port."""

    host_ip: str = Field(min_length=1)

    @field_validator("host_ip")
    @classmethod
    def host_ip_is_connectable(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("guest endpoint host_ip must be a connectable unicast address")
        return value


class RockletArtifactIdentity(StrictModel):
    """Immutable identity of the read-only Rocklet disk attached to a VM."""

    identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    logical_bytes: int = Field(strict=True, gt=0)


class SandboxStartedResult(StrictModel):
    """A synchronous start reply issued only after guest Rocklet readiness."""

    identity: str = Field(min_length=1)
    state: Literal["RUNNING"]
    guest_endpoint: SnapdGuestEndpoint
    vmm_pid: int = Field(strict=True, gt=0, le=4_294_967_295)
    rocklet_artifact: RockletArtifactIdentity


class SandboxStatusResult(StrictModel):
    sandbox_id: str

    state: Literal["STARTING", "RUNNING", "STOPPED", "FAILED"]
    last_error: str | None = None
    result_code: str | None = None
    guest_endpoint: SnapdGuestEndpoint | None = None
    vmm_pid: int | None = Field(default=None, strict=True, gt=0, le=4_294_967_295)
    rocklet_artifact: RockletArtifactIdentity | None = None

    @model_validator(mode="after")
    def running_runtime_has_a_complete_guest_observation(self):
        observation = (self.guest_endpoint, self.vmm_pid, self.rocklet_artifact)
        if self.state == "RUNNING" and any(value is None for value in observation):
            raise ValueError("RUNNING sandbox status requires guest endpoint, VMM PID and Rocklet artifact")
        if self.state != "RUNNING" and any(value is not None for value in observation):
            raise ValueError("non-RUNNING sandbox status must not expose a routable guest endpoint")
        return self


class StopSandboxRequest(StrictModel):
    reason: str = Field(min_length=1)
    deadline_unix_ms: int = Field(ge=0)


class DeleteSandboxRequest(StrictModel):
    pass


class FinalizePublishedSnapshotRequest(StrictModel):
    spec_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MutationAcceptedResult(StrictModel):
    identity: str = Field(min_length=1)
    state: str = Field(min_length=1)


class SnapshotLocalReadyResult(StrictModel):
    snapshot_id: str = Field(min_length=1)
    state: Literal["LOCAL_READY"]
    layer_identities: list[str] = Field(default_factory=list)


class ForkChildResult(StrictModel):
    sandbox_id: str = Field(min_length=1)
    state: Literal["RUNNING", "FAILED", "PENDING"]
    error: str | None = None
    guest_endpoint: SnapdGuestEndpoint | None = None
    vmm_pid: int | None = Field(default=None, strict=True, gt=0, le=4_294_967_295)
    rocklet_artifact: RockletArtifactIdentity | None = None

    @model_validator(mode="after")
    def running_child_has_a_complete_guest_observation(self):
        observation = (self.guest_endpoint, self.vmm_pid, self.rocklet_artifact)
        if self.state == "RUNNING" and any(value is None for value in observation):
            raise ValueError("RUNNING fork child requires guest endpoint, VMM PID and Rocklet artifact")
        if self.state != "RUNNING" and any(value is not None for value in observation):
            raise ValueError("non-running fork child must not expose a routable guest endpoint")
        return self


class ForkLocalReadyResult(SnapshotLocalReadyResult):
    children: list[ForkChildResult]
