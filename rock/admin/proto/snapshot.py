"""Public canonical snapshot API models."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rock.admin.snapd.protocol import MAX_FORK_CHILDREN


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBaseImageRequest(PublicModel):
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    oci_ref: str = Field(min_length=1)
    platform: str = "linux/amd64"


class CreateSnapshotRequest(PublicModel):
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)


class ForkSnapshotRequest(CreateSnapshotRequest):
    count: int = Field(default=1, gt=0, le=MAX_FORK_CHILDREN)
    target_sandbox_ids: list[str] | None = None
    resource_spec: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def target_count_matches(self):
        if self.target_sandbox_ids is not None and len(self.target_sandbox_ids) != self.count:
            raise ValueError("target_sandbox_ids length must equal count")
        if self.target_sandbox_ids is not None and len(set(self.target_sandbox_ids)) != len(self.target_sandbox_ids):
            raise ValueError("target_sandbox_ids must be unique")
        return self


class StartFromSnapshotRequest(PublicModel):
    sandbox_id: str | None = Field(default=None, min_length=1, max_length=128)

    resource_spec: dict[str, Any] = Field(default_factory=dict)


class SnapshotResponse(PublicModel):
    snapshot_id: str
    kind: Literal["BASE_IMAGE", "FULL"]
    state: Literal["CREATING", "LOCAL_READY", "REMOTE_READY", "DELETED", "FAILED"]
    status: Literal["building", "ready", "error", "deleted"]
    source_sandbox_id: str | None = None
    format_version: int
    memory: dict[str, int] | None = None
    rootfs: dict[str, int] | None = None
    created_at: datetime.datetime
    remote_ready_at: datetime.datetime | None = None
    deleted_at: datetime.datetime | None = None


class ForkSnapshotResponse(PublicModel):
    snapshot: SnapshotResponse
    sandbox_ids: list[str]
    children: list[dict[str, Any]]


class StartFromSnapshotResponse(PublicModel):
    sandbox_id: str
    source_snapshot_id: str
    state: str
