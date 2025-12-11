from typing import TypedDict

from rock.deployments.status import PhaseStatus


class SandboxInfo(TypedDict, total=False):
    host_ip: str
    host_name: str
    image: str
    user_id: str
    experiment_id: str
    namespace: str
    sandbox_id: str
    auth_token: str
    phases: dict[str, PhaseStatus]
    port_mapping: dict[int, int]
    create_user_gray_flag: bool
    cpus: float
    memory: str
