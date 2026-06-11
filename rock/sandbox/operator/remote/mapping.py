"""Bidirectional mapping between ROCK domain types and remote sandbox API payloads."""

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

# Remote SandboxState -> ROCK State
_REMOTE_TO_ROCK_STATE: dict[str, State] = {
    "running": State.RUNNING,
    "paused": State.STOPPED,
    "building": State.PENDING,
    "build_failed": State.STOPPED,
}


def map_remote_state(value: str | None) -> State:
    """Translate a remote state string to ROCK State. Unknown values default to PENDING."""
    if not value:
        return State.PENDING
    return _REMOTE_TO_ROCK_STATE.get(value.lower(), State.PENDING)


# Synthetic phase entry for the remote lifecycle.
_REMOTE_PHASE_NAME = "remote_sandbox"
_REMOTE_TO_PHASE_STATUS: dict[str, Status] = {
    "running": Status.SUCCESS,
    "paused": Status.SUCCESS,
    "building": Status.RUNNING,
    "build_failed": Status.FAILED,
}


def _build_phases(raw_state: str | None) -> dict[str, dict[str, str]]:
    """Synthesize a phases dict from the remote state."""
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
    """Convert ROCK memory string ('8g', '4096m') to MiB; return None if invalid."""
    if not memory:
        return None
    try:
        return int(parse_size_to_bytes(memory) // (1024 * 1024))
    except ValueError:
        return None


def to_new_sandbox_payload(config: DockerDeploymentConfig) -> dict[str, Any]:
    """Build the NewSandbox JSON body for POST /sandboxes."""
    payload: dict[str, Any] = {}

    image = (config.image or "").strip()
    if image:
        payload["fromImage"] = image

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

    # autoPause=true: TTL expiry -> paused (not kill), aligns with ROCK STOPPED state
    payload["autoPause"] = True

    metadata: dict[str, str] = {
        "rock.container_name": config.container_name or "",
    }
    # extended_params -> remote metadata (string-only)
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
    """Build SandboxInfo from a POST /sandboxes response."""
    info: SandboxInfo = {
        "sandbox_id": body.get("sandboxID", ""),
        "host_name": body.get("sandboxID", ""),
        "host_ip": "",
        "image": (config.image if config else "") or "",
        "state": State.PENDING,
        "extended_params": {
            "remote.sandbox_domain": body.get("domain") or "",
            "remote.traffic_access_token": body.get("trafficAccessToken") or "",
        },
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
        "image": "",
        "state": map_remote_state(raw_state),
        "extended_params": {
            "remote.sandbox_domain": body.get("domain") or "",
        },
        "start_time": body.get("startedAt") or "",
        "port_mapping": {},
        "phases": _build_phases(raw_state),
    }
    if body.get("cpuCount") is not None:
        info["cpus"] = float(body["cpuCount"])
    if body.get("memoryMB") is not None:
        info["memory"] = f"{int(body['memoryMB'])}m"
    return info
