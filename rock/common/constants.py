from enum import Enum

GET_STATUS_SWITCH = "get_status_v2_enabled"
KATA_RUNTIME_SWITCH = "use_kata_enabled"
SUPPORT_KATA_SWITCH = "support_kata_enabled"
CPU_OVERCOMMIT_HEADROOM_KEY = "cpu_overcommit_headroom"
CPU_OVERCOMMIT_ALLOWED_KEYS_KEY = "cpu_overcommit_allowed_keys"
KATA_DIND_DISK_SIZE_KEY = "kata_dind_disk_size"
SANDBOX_DISK_LIMIT_ROOTFS_KEY = "sandbox_disk_limit_rootfs"
SANDBOX_DISK_OVERCOMMIT_RATIO_KEY = "sandbox_disk_overcommit_ratio"
EXTRA_ACCELERATOR_TYPES_KEY = "extra_accelerator_types"
PID_PREFIX = "PIDSTART"
PID_SUFFIX = "PIDEND"
SCHEDULER_LOG_NAME = "scheduler.log"


class DeploymentHookStep(str, Enum):
    """Hook step messages used to coordinate between deployment and hooks.

    Inherits from `str` so values can be compared directly with plain strings.
    """

    PULLING_IMAGE = "Pulling docker image"
    STARTING_RUNTIME = "Starting runtime"


class StopReason(str, Enum):
    """Why a sandbox was stopped. Propagated through the SandboxManager → Operator → Actor
    stop chain so the actor-side lifecycle summary can distinguish user-initiated stops
    from auto-cleanup of expired sandboxes.
    """

    MANUAL = "manual"
    EXPIRED = "expired"


class DeleteReason(str, Enum):
    """Why a sandbox was deleted. Distinguishes operator-initiated /delete calls from
    background scanner cleanups driven by ``auto_delete_seconds``.
    """

    MANUAL = "manual"
    # TODO: implement background auto-delete scan driven by auto_delete_seconds
    EXPIRED = "expired"
    # `--rm` containers: cascade STOPPED → DELETED on stop since the container is already gone.
    IMMEDIATE = "immediate"
