from enum import Enum, IntEnum


class Status(Enum):
    WAITING = "waiting"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class Port(IntEnum):
    SSH = 22
    PROXY = 22555
    SERVER = 8080
