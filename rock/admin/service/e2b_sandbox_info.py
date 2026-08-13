import datetime
import math
from ipaddress import ip_address
from typing import Literal

from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStatusResponse
from rock.common.constants import E2B_CLIENT_ID, E2B_ENVD_VERSION, E2B_SANDBOX_IP_METADATA_KEY, E2B_STATE_BY_ROCK_STATE
from rock.sdk.common.exceptions import E2BSandboxNotFoundError
from rock.utils.format import parse_size_to_bytes


def e2b_sandbox_info_fields(sandbox_id: str, sandbox_status: SandboxStatusResponse) -> dict[str, object]:
    state = _e2b_state(sandbox_status.state)
    if state is None:
        raise E2BSandboxNotFoundError(f"Sandbox {sandbox_id} not found")
    end_at = (
        sandbox_status.auto_stop_time
        if state == "running"
        else sandbox_status.auto_delete_time or sandbox_status.archive_time
    )

    return {
        "sandboxID": sandbox_id,
        "metadata": _metadata_with_sandbox_ip(
            sandbox_id,
            sandbox_status.metadata,
            sandbox_status.host_ip,
        ),
        "state": state,
        "clientID": E2B_CLIENT_ID,
        "templateID": str(sandbox_status.image),
        "envdVersion": E2B_ENVD_VERSION,
        "cpuCount": max(1, math.ceil(float(sandbox_status.cpus))),
        "memoryMB": parse_size_to_bytes(str(sandbox_status.memory)) // (1024**2),
        "diskSizeMB": parse_size_to_bytes(str(sandbox_status.disk)) // (1024**2),
        "startedAt": _iso8601_timestamp(
            sandbox_id,
            "start time",
            sandbox_status.start_time or sandbox_status.create_time,
        ),
        "endAt": _iso8601_timestamp(sandbox_id, "end time", end_at),
    }


def _metadata_with_sandbox_ip(
    sandbox_id: str,
    metadata: object,
    host_ip: object,
) -> dict[str, str]:
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
    ):
        raise ValueError(f"Sandbox {sandbox_id} metadata is invalid")
    if not isinstance(host_ip, str) or not host_ip.strip():
        raise ValueError(f"Sandbox {sandbox_id} IP is missing")
    ip_address(host_ip)
    return {**metadata, E2B_SANDBOX_IP_METADATA_KEY: host_ip}


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


def _e2b_state(value: object) -> Literal["running", "paused"] | None:
    try:
        rock_state = value if isinstance(value, State) else State(value)
        return E2B_STATE_BY_ROCK_STATE.get(rock_state.value)
    except (TypeError, ValueError):
        return None
