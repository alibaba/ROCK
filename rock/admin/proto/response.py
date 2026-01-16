from pydantic import BaseModel

from rock.actions import SandboxResponse
from rock.actions.sandbox.response import State


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
    cpus: float | None = None
    memory: str | None = None
