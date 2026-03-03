from enum import Enum

GET_STATUS_SWITCH = "get_status_v2_enabled"
KATA_RUNTIME_SWITCH = "kata_runtime_enabled"
PID_PREFIX = "PIDSTART"
PID_SUFFIX = "PIDEND"
SCHEDULER_LOG_NAME = "scheduler.log"


class DeploymentHookStep(str, Enum):
    """Hook step messages used to coordinate between deployment and hooks.

    Inherits from `str` so values can be compared directly with plain strings.
    """

    PULLING_IMAGE = "Pulling docker image"
    STARTING_RUNTIME = "Starting runtime"
