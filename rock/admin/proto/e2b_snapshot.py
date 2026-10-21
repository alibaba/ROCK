"""Typed wire models for the E2B snapshot/template compatibility surface."""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from rock.admin.proto.response import E2BCreateSandboxResponse
from rock.admin.snapd.protocol import MAX_FORK_CHILDREN


class E2BWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class E2BSnapshot(E2BWireModel):
    snapshot_id: str = Field(alias="snapshotID")
    names: list[str] = Field(default_factory=list)


class E2BCreateSnapshotRequest(E2BWireModel):
    name: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def only_unnamed_snapshots_are_supported(cls, value: str | None):
        if value:
            raise ValueError("named snapshots are not supported")
        return value


class E2BForkRequest(E2BWireModel):
    timeout: StrictInt = Field(default=15, gt=0, le=86_400)
    count: StrictInt = Field(default=1, ge=1, le=MAX_FORK_CHILDREN)


class E2BForkError(E2BWireModel):
    code: int
    message: str


class E2BForkResult(E2BWireModel):
    sandbox: E2BCreateSandboxResponse | None = None
    error: E2BForkError | None = None

    @model_validator(mode="after")
    def exactly_one_result(self):
        if (self.sandbox is None) == (self.error is None):
            raise ValueError("fork result must contain exactly one of sandbox or error")
        return self


class E2BCreateTemplateRequest(E2BWireModel):
    name: str | None = Field(default=None, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=64)
    alias: str | None = Field(default=None, max_length=128)
    team_id: str | None = Field(default=None, alias="teamID", max_length=128)
    cpu_count: StrictInt = Field(default=2, alias="cpuCount", ge=1, le=1024)
    memory_mb: StrictInt = Field(default=8192, alias="memoryMB", ge=128)


class E2BCreateTemplateResponse(E2BWireModel):
    template_id: str = Field(alias="templateID")
    build_id: str = Field(alias="buildID")
    public: bool = False
    names: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class E2BStartTemplateBuildRequest(E2BWireModel):
    from_image: str = Field(alias="fromImage", min_length=1, max_length=2048)


class E2BTemplateBuild(E2BWireModel):
    build_id: str = Field(alias="buildID")
    status: Literal["building", "ready", "error"]
    created_at: datetime.datetime = Field(alias="createdAt")
    updated_at: datetime.datetime = Field(alias="updatedAt")
    cpu_count: int = Field(alias="cpuCount", ge=1)
    memory_mb: int = Field(alias="memoryMB", ge=1)
    disk_size_mb: int = Field(alias="diskSizeMB", ge=0)
    envd_version: str = Field(alias="envdVersion")
    finished_at: datetime.datetime | None = Field(default=None, alias="finishedAt")


class E2BTemplate(E2BWireModel):
    template_id: str = Field(alias="templateID")
    public: bool = False
    aliases: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    created_at: datetime.datetime = Field(alias="createdAt")
    updated_at: datetime.datetime = Field(alias="updatedAt")
    last_spawned_at: datetime.datetime | None = Field(default=None, alias="lastSpawnedAt")
    spawn_count: int = Field(default=0, alias="spawnCount", ge=0)
    builds: list[E2BTemplateBuild]
