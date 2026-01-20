from pydantic import BaseModel

from rock.actions import SandboxResponse
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo, SandboxListItem


class SandboxStartResponse(SandboxResponse):
    sandbox_id: str | None = None
    host_name: str | None = None
    host_ip: str | None = None
    cpus: float | None = None
    memory: str | None = None


# TODO: inherit from SandboxStartResponse
class SandboxStatusResponse(BaseModel):
    sandbox_id: str = None
    status: dict = None
    state: State | None = None
    port_mapping: dict = None
    host_name: str | None = None
    host_ip: str | None = None
    is_alive: bool = True
    image: str | None = None
    gateway_version: str | None = None
    swe_rex_version: str | None = None
    user_id: str | None = None
    experiment_id: str | None = None
    namespace: str | None = None
    cpus: float | None = None
    memory: str | None = None

    @classmethod
    def from_sandbox_info(cls, sandbox_info: "SandboxInfo") -> "SandboxStatusResponse":
        return cls(
            sandbox_id=sandbox_info.get("sandbox_id", ""),
            status=sandbox_info.get("phases", {}),
            state=sandbox_info.get("state"),
            port_mapping=sandbox_info.get("port_mapping", {}),
            host_ip=sandbox_info.get("host_ip"),
            host_name=sandbox_info.get("host_name"),
            image=sandbox_info.get("image"),
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
        )


class SandboxListStatusResponse(SandboxStatusResponse):
    rock_authorization_encrypted: str | None = None

    @classmethod
    def from_sandbox_info(cls, sandbox_info: "SandboxListItem") -> "SandboxListStatusResponse":
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
