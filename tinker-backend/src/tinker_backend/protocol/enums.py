"""Domain enumerations for Tinker backend state machines and action kinds."""

from enum import StrEnum


class FutureState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class RuntimeState(StrEnum):
    CREATING = "creating"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    LAUNCH_NOT_IMPLEMENTED = "launch_not_implemented"
    FAILED = "failed"
    STOPPED = "stopped"


class ActionKind(StrEnum):
    CREATE_MODEL = "create_model"
    CREATE_SAMPLING_SESSION = "create_sampling_session"
    INIT_TASK_ENV = "init_task_env"
    GET_STEP = "get_step"
    SAMPLE = "sample"
    CLOSE_RUNTIME = "close_runtime"
    CLOSE_ROCK_ADAPTER = "close_rock_adapter"
    FORWARD_BACKWARD = "forward_backward"
    FORWARD = "forward"
    OPTIM_STEP = "optim_step"
    SAVE_WEIGHTS = "save_weights"
    PUBLISH_TO_SAMPLER = "publish_to_sampler"
    LOAD_WEIGHTS = "load_weights"


class ActionState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


class EnvironmentState(StrEnum):
    PENDING_INIT = "pending_init"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
