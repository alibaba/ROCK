from datetime import timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rock.actions import FileEntry, FileEntryType, SandboxResponse
from rock.actions.sandbox.response import State, StateTransitionRecord
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.request import TaskSetSpec
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.e2b import E2BConnectCode


class E2BCreateSandboxResponse(BaseModel):
    sandbox_id: str = Field(alias="sandboxID")
    envd_version: str = Field(alias="envdVersion")
    envd_access_token: str | None = Field(default=None, alias="envdAccessToken")
    client_id: str = Field(alias="clientID")
    template_id: str = Field(alias="templateID")


class E2BConnectErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: E2BConnectCode
    message: str


class E2BConnectEndResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    error: E2BConnectErrorPayload | None = None


class E2BFileEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

    name: str
    type: Literal["FILE_TYPE_FILE", "FILE_TYPE_DIRECTORY", "FILE_TYPE_UNSPECIFIED"]
    path: str
    size: str
    mode: int
    permissions: str
    owner: str
    group: str
    modified_time: str = Field(alias="modifiedTime")
    symlink_target: str | None = Field(default=None, alias="symlinkTarget")

    @classmethod
    def from_file_entry(cls, entry: FileEntry) -> "E2BFileEntryResponse":
        entry_type = {
            FileEntryType.FILE: "FILE_TYPE_FILE",
            FileEntryType.DIR: "FILE_TYPE_DIRECTORY",
        }.get(entry.type, "FILE_TYPE_UNSPECIFIED")
        modified_time = entry.modified_time
        if modified_time.tzinfo is None:
            modified_time = modified_time.replace(tzinfo=timezone.utc)
        return cls(
            name=entry.name,
            type=entry_type,
            path=entry.path,
            size=str(entry.size),
            mode=entry.mode,
            permissions=entry.permissions,
            owner=entry.owner,
            group=entry.group,
            modifiedTime=modified_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            symlinkTarget=entry.symlink_target,
        )


class E2BListDirResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entries: list[E2BFileEntryResponse]


class E2BStatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entry: E2BFileEntryResponse


class E2BWrittenFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    type: Literal["file"] = "file"
    path: str

    @classmethod
    def from_path(cls, path: str) -> "E2BWrittenFileResponse":
        return cls(name=PurePosixPath(path).name, path=path)


class E2BProcessStartEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pid: int = Field(gt=0)


class E2BProcessDataEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stdout: str | None = None
    stderr: str | None = None


class E2BProcessEndEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

    exit_code: int = Field(alias="exitCode")
    exited: bool
    error: str | None = None


class E2BProcessKeepaliveEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class E2BProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    start: E2BProcessStartEvent | None = None
    data: E2BProcessDataEvent | None = None
    end: E2BProcessEndEvent | None = None
    keepalive: E2BProcessKeepaliveEvent | None = None


class E2BProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event: E2BProcessEvent

    @classmethod
    def started(cls, pid: int) -> "E2BProcessResponse":
        return cls(event=E2BProcessEvent(start=E2BProcessStartEvent(pid=pid)))

    @classmethod
    def stdout(cls, content: str) -> "E2BProcessResponse":
        return cls(event=E2BProcessEvent(data=E2BProcessDataEvent(stdout=content)))

    @classmethod
    def stderr(cls, content: str) -> "E2BProcessResponse":
        return cls(event=E2BProcessEvent(data=E2BProcessDataEvent(stderr=content)))

    @classmethod
    def keepalive_event(cls) -> "E2BProcessResponse":
        return cls(event=E2BProcessEvent(keepalive=E2BProcessKeepaliveEvent()))

    @classmethod
    def ended(cls, exit_code: int) -> "E2BProcessResponse":
        error = f"exit status {exit_code}" if exit_code != 0 else None
        return cls(
            event=E2BProcessEvent(
                end=E2BProcessEndEvent(exitCode=exit_code, exited=True, error=error),
            )
        )


class E2BSandboxInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sandbox_id: str = Field(alias="sandboxID")
    metadata: dict[str, str]
    state: Literal["running", "paused"]
    client_id: str = Field(alias="clientID")
    template_id: str = Field(alias="templateID")
    envd_version: str = Field(alias="envdVersion")
    cpu_count: int = Field(alias="cpuCount")
    memory_mb: int = Field(alias="memoryMB")
    disk_size_mb: int = Field(alias="diskSizeMB")
    started_at: str = Field(alias="startedAt")
    end_at: str = Field(alias="endAt")


class E2BSandboxVolumeMount(BaseModel):
    name: str
    path: str


class E2BListedSandbox(E2BSandboxInfo):
    alias: str | None = None
    volume_mounts: list[E2BSandboxVolumeMount] | None = Field(default=None, alias="volumeMounts")


class SandboxStartResponse(SandboxResponse):
    sandbox_id: str | None = None
    source_snapshot_id: str | None = None
    host_name: str | None = None
    host_ip: str | None = None
    cpus: float | None = None
    memory: str | None = None
    disk: str | None = None
    disk_limit_rootfs: str | None = Field(default=None, deprecated="Use 'disk' instead")


# TODO: inherit from SandboxStartResponse
class SandboxStatusResponse(BaseModel):
    sandbox_id: str = None
    source_snapshot_id: str | None = None
    status: dict | None = None
    state: State | None = None
    port_mapping: dict | None = None
    host_name: str | None = None
    host_ip: str | None = None
    is_alive: bool = True
    image: str | None = None
    metadata: dict[str, str] | None = None
    gateway_version: str | None = None
    swe_rex_version: str | None = None
    user_id: str | None = None
    experiment_id: str | None = None
    namespace: str | None = None
    cpus: float | None = None
    memory: str | None = None
    num_gpus: float | None = None
    accelerator_type: str | None = None
    disk: str | None = None
    disk_limit_rootfs: str | None = Field(default=None, deprecated="Use 'disk' instead")
    start_time: str | None = None
    stop_time: str | None = None
    create_time: str | None = None
    archive_time: str | None = None
    delete_time: str | None = None
    auto_stop_time: str | None = None
    auto_archive_time: str | None = None
    auto_delete_time: str | None = None
    state_history: list[StateTransitionRecord] = []

    @classmethod
    def from_sandbox_info(cls, sandbox_info: "SandboxInfo") -> "SandboxStatusResponse":
        auto_stop_time, auto_archive_time, auto_delete_time = SandboxTimeoutHelper.auto_transition_times_for_status(
            sandbox_info.get("state"),
            sandbox_info,
        )
        return cls(
            sandbox_id=sandbox_info.get("sandbox_id", ""),
            source_snapshot_id=sandbox_info.get("source_snapshot_id"),
            status=sandbox_info.get("phases", {}),
            state=sandbox_info.get("state"),
            port_mapping=sandbox_info.get("port_mapping", {}),
            host_ip=sandbox_info.get("host_ip"),
            host_name=sandbox_info.get("host_name"),
            image=sandbox_info.get("image"),
            metadata=sandbox_info.get("metadata"),
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
            num_gpus=sandbox_info.get("num_gpus"),
            accelerator_type=sandbox_info.get("accelerator_type"),
            disk=sandbox_info.get("disk"),
            disk_limit_rootfs=sandbox_info.get("disk"),
            start_time=sandbox_info.get("start_time"),
            stop_time=sandbox_info.get("stop_time"),
            create_time=sandbox_info.get("create_time"),
            archive_time=sandbox_info.get("archive_time"),
            delete_time=sandbox_info.get("delete_time"),
            auto_stop_time=auto_stop_time,
            auto_archive_time=auto_archive_time,
            auto_delete_time=auto_delete_time,
            state_history=sandbox_info.get("state_history", []),
        )


class SandboxListStatusResponse(SandboxStatusResponse):
    rock_authorization_encrypted: str | None = None

    @classmethod
    def from_sandbox_info(cls, sandbox_info: "SandboxInfo") -> "SandboxListStatusResponse":
        base_data = super().from_sandbox_info(sandbox_info)
        base_dict = base_data.model_dump()
        base_dict["rock_authorization_encrypted"] = sandbox_info.get("rock_authorization_encrypted", None)
        return cls(**base_dict)


class BatchSandboxStatusResponse(SandboxResponse):
    statuses: list[SandboxStatusResponse] | None = None


class SandboxListResponse(SandboxResponse):
    items: list[SandboxListStatusResponse] = []
    total: int = 0
    has_more: bool = False


class TaskSetMetadata(BaseModel):
    tasksetId: str
    creationTimestamp: float


class TaskSetStatusModel(BaseModel):
    phase: str
    assignedPod: str = ""
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    startTime: float | None = None
    completionTime: float | None = None
    conditions: list[dict] | None = None


class TaskMetadata(BaseModel):
    taskId: str
    tasksetId: str
    creationTimestamp: float


class TaskStatusModel(BaseModel):
    phase: str
    startTime: float | None = None
    completionTime: float | None = None
    conditions: list[dict] | None = None
    status: list[dict] | None = None


class TaskResponse(BaseModel):
    metadata: TaskMetadata
    spec: dict
    status: TaskStatusModel


class TaskSetResponse(BaseModel):
    metadata: TaskSetMetadata
    spec: "TaskSetSpec"
    status: TaskSetStatusModel
    tasks: list[TaskResponse] | None = None
