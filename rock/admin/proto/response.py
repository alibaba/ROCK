import datetime
import math
from ipaddress import ip_address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rock.actions import SandboxResponse
from rock.actions.sandbox.response import State, StateTransitionRecord
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.request import TaskSetSpec
from rock.common.constants import E2B_CLIENT_ID, E2B_ENVD_VERSION, E2B_SANDBOX_IP_METADATA_KEY, E2B_STATE_BY_ROCK_STATE
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import E2BSandboxNotFoundError
from rock.utils.format import parse_size_to_bytes


class E2BCreateSandboxResponse(BaseModel):
    sandbox_id: str = Field(alias="sandboxID")
    envd_version: str = Field(alias="envdVersion")
    client_id: str = Field(alias="clientID")
    template_id: str = Field(alias="templateID")


class SandboxStartResponse(SandboxResponse):
    sandbox_id: str | None = None
    host_name: str | None = None
    host_ip: str | None = None
    cpus: float | None = None
    memory: str | None = None
    disk: str | None = None
    disk_limit_rootfs: str | None = Field(default=None, deprecated="Use 'disk' instead")


# TODO: inherit from SandboxStartResponse
class SandboxStatusResponse(BaseModel):
    sandbox_id: str = None
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


class E2BSandboxDetail(BaseModel):
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

    @staticmethod
    def _state(sandbox_id: str, state: State | str | None) -> Literal["running", "paused"]:
        try:
            rock_state = state if isinstance(state, State) else State(state)
            return E2B_STATE_BY_ROCK_STATE[rock_state.value]
        except (KeyError, TypeError, ValueError):
            raise E2BSandboxNotFoundError(f"Sandbox {sandbox_id} not found") from None

    @staticmethod
    def _iso8601_timestamp(sandbox_id: str, field: str, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Sandbox {sandbox_id} {field} is invalid")
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Sandbox {sandbox_id} {field} is invalid") from None
        if parsed.tzinfo is None:
            raise ValueError(f"Sandbox {sandbox_id} {field} must include a timezone")
        return parsed.isoformat(timespec="seconds")

    @classmethod
    def from_sandbox_status(
        cls,
        sandbox_id: str,
        sandbox_status: SandboxStatusResponse,
    ) -> "E2BSandboxDetail":
        state = cls._state(sandbox_id, sandbox_status.state)
        end_at = (
            sandbox_status.auto_stop_time
            if state == "running"
            else sandbox_status.auto_delete_time or sandbox_status.archive_time
        )

        metadata = sandbox_status.metadata
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise ValueError(f"Sandbox {sandbox_id} metadata is invalid")

        host_ip = sandbox_status.host_ip
        if not isinstance(host_ip, str) or not host_ip.strip():
            raise ValueError(f"Sandbox {sandbox_id} IP is missing")
        ip_address(host_ip)

        return cls(
            sandboxID=sandbox_id,
            metadata={**metadata, E2B_SANDBOX_IP_METADATA_KEY: host_ip},
            state=state,
            clientID=E2B_CLIENT_ID,
            templateID=str(sandbox_status.image),
            envdVersion=E2B_ENVD_VERSION,
            cpuCount=max(1, math.ceil(float(sandbox_status.cpus))),
            memoryMB=parse_size_to_bytes(str(sandbox_status.memory)) // (1024**2),
            diskSizeMB=parse_size_to_bytes(str(sandbox_status.disk)) // (1024**2),
            startedAt=cls._iso8601_timestamp(
                sandbox_id,
                "start time",
                sandbox_status.start_time or sandbox_status.create_time,
            ),
            endAt=cls._iso8601_timestamp(sandbox_id, "end time", end_at),
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
