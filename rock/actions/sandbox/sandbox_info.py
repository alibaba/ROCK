from typing import Any, TypedDict

from rock.actions.sandbox.response import State
from rock.deployments.status import PhaseStatus


class SandboxInfo(TypedDict, total=False):
    host_ip: str
    host_name: str
    image: str
    user_id: str
    experiment_id: str
    namespace: str
    cluster_name: str
    sandbox_id: str
    auth_token: str
    rock_authorization_encrypted: str
    phases: dict[str, PhaseStatus]
    state: State
    port_mapping: dict[int, int]
    create_user_gray_flag: bool
    cpus: float
    memory: str
    disk: str
    create_time: str
    start_time: str
    stop_time: str
    delete_time: str
    extended_params: dict[str, str]


_SANDBOX_INFO_KEYS = frozenset(SandboxInfo.__annotations__.keys())


def pick_sandbox_info_fields(data: dict[str, Any]) -> "SandboxInfo":
    """Return a dict containing only keys declared in :class:`SandboxInfo`.

    Used by ``SandboxMetaStore`` to keep DB-only columns (e.g. ``spec`` /
    ``status``, surfaced by ``SandboxRecord.to_dict()`` on the DB-fallback
    read path) out of the Redis alive key.
    """
    return {k: v for k, v in data.items() if k in _SANDBOX_INFO_KEYS}  # type: ignore[return-value]
