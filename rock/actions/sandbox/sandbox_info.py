from typing import Any, TypedDict

from rock.actions.sandbox.response import State, StateTransitionRecord
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
    num_gpus: float
    accelerator_type: str
    disk: str
    create_time: str
    start_time: str
    stop_time: str
    delete_time: str
    archive_time: str
    auto_transition_state: State
    auto_transition_time: str
    auto_archive_seconds: int | None
    auto_delete_seconds: int | None
    archive_prefix: str
    registry_namespace: str
    extended_params: dict[str, str]
    state_history: list[StateTransitionRecord]


_SANDBOX_INFO_KEYS = frozenset(SandboxInfo.__annotations__.keys())


def pick_sandbox_info_fields(data: dict[str, Any]) -> "SandboxInfo":
    """Return a dict containing only keys declared in :class:`SandboxInfo`.

    Used by ``SandboxMetaStore`` to keep DB-only columns (e.g. ``spec`` /
    ``status``, surfaced by ``SandboxRecord.to_dict()`` on the DB-fallback
    read path) out of the Redis alive key.
    """
    return {k: v for k, v in data.items() if k in _SANDBOX_INFO_KEYS}  # type: ignore[return-value]
