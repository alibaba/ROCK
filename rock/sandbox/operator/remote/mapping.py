"""Bidirectional mapping between ROCK domain types and Infra OpenAPI payloads.

Centralized so RemoteOperator and RemoteClient never construct or interpret
Infra payloads directly. Each function is small and pure to keep transcoding
testable.
"""

from __future__ import annotations

from typing import Any

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Status
from rock.utils.format import parse_size_to_bytes


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------

# Infra ``SandboxState`` enum (per OpenAPI):
#   running | paused | building | build_failed
# Mapped to ROCK's ``State`` so SandboxManager.get_status sees consistent
# semantics across operators.
_REMOTE_TO_ROCK_STATE: dict[str, State] = {
    "running": State.RUNNING,
    "paused": State.STOPPED,
    "building": State.PENDING,
    "build_failed": State.STOPPED,
}


def map_remote_state(value: str | None) -> State:
    """Translate a remote state string to ROCK ``State``.

    Unknown values fall back to ``PENDING`` to keep the read path lenient
    against future schema additions; the caller is expected to log the
    raw value so divergence is visible.
    """
    if not value:
        return State.PENDING
    return _REMOTE_TO_ROCK_STATE.get(value.lower(), State.PENDING)


# Single synthetic phase that mirrors the remote lifecycle into the
# ``phases`` dict that Ray/K8s operators populate. We keep one entry so
# downstream code (metrics decorator, status response) has a uniform
# shape regardless of operator. ``message`` carries the raw remote state
# verbatim for debuggability.
_REMOTE_PHASE_NAME = "remote_sandbox"
_REMOTE_TO_PHASE_STATUS: dict[str, Status] = {
    "running": Status.SUCCESS,
    "paused": Status.SUCCESS,
    "building": Status.RUNNING,
    "build_failed": Status.FAILED,
}


def _build_phases(raw_state: str | None) -> dict[str, dict[str, str]]:
    """Synthesize a Ray-compatible ``phases`` dict from the remote state.

    Empty/unknown states map to ``WAITING`` so SandboxStatusResponse stays
    a valid dict (Pydantic rejects ``None``).
    """
    state_str = (raw_state or "").lower()
    phase_status = _REMOTE_TO_PHASE_STATUS.get(state_str, Status.WAITING)
    return {
        _REMOTE_PHASE_NAME: {
            "status": phase_status.value,
            "message": state_str or "pending",
        }
    }


# ---------------------------------------------------------------------------
# Outbound: DockerDeploymentConfig -> NewSandbox JSON body
# ---------------------------------------------------------------------------


def _memory_str_to_mb(memory: str | None) -> int | None:
    """Convert ROCK's free-form memory string ('8g', '4096m') to MiB int.

    Returns ``None`` if the value is empty or cannot be parsed; the remote
    schema enforces ``minimum: 128`` so we let the server reject invalid
    values rather than silently clamping here.
    """
    if not memory:
        return None
    try:
        return int(parse_size_to_bytes(memory) // (1024 * 1024))
    except ValueError:
        return None


def to_new_sandbox_payload(
    config: DockerDeploymentConfig,
    *,
    default_template_id: str = "",
) -> dict[str, Any]:
    """Build the ``NewSandbox`` JSON body for ``POST /sandboxes``.

    Resolution rules between ROCK ``DockerDeploymentConfig.image`` and the
    remote API's mutually-exclusive ``templateID`` / ``fromImage`` pair:

    - When the image looks like an OCI reference (contains ``/`` or ``:``),
      use ``fromImage`` so the remote builds an implicit template.
    - Otherwise treat the image string as an explicit ``templateID``.
    - When the resolved value is empty and ``default_template_id`` is
      configured, fall back to the default template.

    Memory is converted to MiB. ``timeout`` reflects the auto-clear window
    in seconds (the remote's TTL semantics).
    """
    image = (config.image or "").strip()
    payload: dict[str, Any] = {}

    if image:
        if "/" in image or ":" in image:
            payload["fromImage"] = image
        else:
            payload["templateID"] = image
    elif default_template_id:
        payload["templateID"] = default_template_id

    if config.cpus is not None:
        # Remote CPUCount is int>=1; ROCK allows fractional cpus, so round up.
        cpu_count = max(1, int(config.cpus + 0.999))
        payload["cpuCount"] = cpu_count

    memory_mb = _memory_str_to_mb(config.memory)
    if memory_mb is not None:
        payload["memoryMB"] = memory_mb

    # Auto-clear window in minutes -> seconds for the remote TTL.
    if config.auto_clear_time_minutes:
        payload["timeout"] = int(config.auto_clear_time_minutes) * 60

    metadata: dict[str, str] = {
        "rock.container_name": config.container_name or "",
    }
    # Free-form ROCK extension dict goes into remote metadata (string-only).
    for key, value in (config.extended_params or {}).items():
        if value is None:
            continue
        metadata[f"rock.ext.{key}"] = str(value)
    payload["metadata"] = {k: v for k, v in metadata.items() if v}

    return payload


# ---------------------------------------------------------------------------
# Inbound: remote JSON -> SandboxInfo
# ---------------------------------------------------------------------------


def from_sandbox_response(
    body: dict[str, Any],
    *,
    config: DockerDeploymentConfig | None = None,
) -> SandboxInfo:
    """Build SandboxInfo from a ``POST /sandboxes`` response (``Sandbox``).

    The response only carries identity and addressing; lifecycle timestamps
    and state are populated by the subsequent GET. ``host_ip`` is left empty
    on purpose — the Addressing Layer reads ``sandbox_domain`` instead.
    """
    info: SandboxInfo = {
        "sandbox_id": body.get("sandboxID", ""),
        "host_name": body.get("sandboxID", ""),
        "host_ip": "",
        "image": (config.image if config else "") or body.get("templateID", "") or "",
        "state": State.PENDING,
        "sandbox_domain": body.get("domain") or "",
        "envd_access_token": body.get("envdAccessToken") or "",
        "traffic_access_token": body.get("trafficAccessToken") or "",
        # Remote sandboxes are reached through ``sandbox_domain`` rather than
        # host ports, but downstream consumers expect a dict — keep an empty
        # mapping for shape parity with Ray/K8s.
        "port_mapping": {},
        "phases": _build_phases(body.get("state")),
    }
    if config is not None:
        info["cpus"] = float(config.cpus) if config.cpus is not None else 0.0
        info["memory"] = config.memory or ""
    return info


def from_sandbox_detail(body: dict[str, Any]) -> SandboxInfo:
    """Build SandboxInfo from a ``GET /sandboxes/{id}`` response (``SandboxDetail``)."""
    raw_state = body.get("state")
    info: SandboxInfo = {
        "sandbox_id": body.get("sandboxID", ""),
        "host_name": body.get("sandboxID", ""),
        "host_ip": "",
        "image": body.get("templateID", "") or "",
        "state": map_remote_state(raw_state),
        "sandbox_domain": body.get("domain") or "",
        "envd_access_token": body.get("envdAccessToken") or "",
        "start_time": body.get("startedAt") or "",
        "port_mapping": {},
        "phases": _build_phases(raw_state),
    }
    if body.get("cpuCount") is not None:
        info["cpus"] = float(body["cpuCount"])
    if body.get("memoryMB") is not None:
        info["memory"] = f"{int(body['memoryMB'])}m"
    return info
