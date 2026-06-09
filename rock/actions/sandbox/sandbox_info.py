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
    disk_limit_rootfs: str
    create_time: str
    start_time: str
    stop_time: str
    delete_time: str
    extended_params: dict[str, str]
    # ----- Multi-operator routing & remote addressing (TypedDict total=False) -----
    operator_name: str
    """Name of the operator that submitted this sandbox (one of
    ``OPERATOR_CONFIG_KEYS``: ``ray``/``k8s``/``remote``). Set once at submit
    time and used by SandboxManager to dispatch GET/stop/delete to the same
    operator. Absent on legacy records, in which case the registry default is
    used."""
    sandbox_domain: str
    """Domain returned by the remote sandbox API (the ``SandboxDetail.domain``
    field). Used by the Addressing Layer to construct the proxy target URL for
    sandbox-data-plane requests. Empty for ray/k8s sandboxes."""
    envd_access_token: str
    """Token sent as ``X-Access-Token`` to the remote envd. Empty for
    ray/k8s."""
    traffic_access_token: str
    """Optional traffic token returned by the remote API. Empty when not
    used."""


_SANDBOX_INFO_KEYS = frozenset(SandboxInfo.__annotations__.keys())


def pick_sandbox_info_fields(data: dict[str, Any]) -> "SandboxInfo":
    """Return a dict containing only keys declared in :class:`SandboxInfo`.

    Used by ``SandboxMetaStore`` to keep DB-only columns (e.g. ``spec`` /
    ``status``, surfaced by ``SandboxRecord.to_dict()`` on the DB-fallback
    read path) out of the Redis alive key.
    """
    return {k: v for k, v in data.items() if k in _SANDBOX_INFO_KEYS}  # type: ignore[return-value]
