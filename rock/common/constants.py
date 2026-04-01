from enum import Enum

GET_STATUS_SWITCH = "get_status_v2_enabled"
KATA_RUNTIME_SWITCH = "use_kata_enabled"
SUPPORT_KATA_SWITCH = "support_kata_enabled"
CPU_PREEMPT_SWITCH = "cpu_preempt_enabled"
KATA_DIND_DISK_SIZE_KEY = "kata_dind_disk_size"
PID_PREFIX = "PIDSTART"
PID_SUFFIX = "PIDEND"
SCHEDULER_LOG_NAME = "scheduler.log"


class DeploymentHookStep(str, Enum):
    """Hook step messages used to coordinate between deployment and hooks.

    Inherits from `str` so values can be compared directly with plain strings.
    """

    PULLING_IMAGE = "Pulling docker image"
    STARTING_RUNTIME = "Starting runtime"
