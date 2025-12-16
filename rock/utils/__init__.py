from contextvars import ContextVar

from .concurrent_helper import AsyncAtomicInt, AsyncSafeDict, RayUtil, Timer, get_executor, run_until_complete, timeout
from .data import (
    FileUtil,
    ListUtil,
)
from .docker import (
    DockerUtil,
    ImageUtil,
)
from .exception import (
    handle_exceptions,
)
from .http import (
    HttpUtils,
    wait_until_alive,
)
from .importer import (
    can_import_class,
    safe_import_class,
)
from .retry import (
    retry_async,
)
from .system import (
    extract_nohup_pid,
    find_free_port,
    get_host_ip,
    get_uniagent_endpoint,
    release_port,
    run_command_with_output,
    run_shell_command,
)

ENV_POOL = {}
SANDBOX_ID = "sandbox_id"
ROUTE_KEY = "ROUTE-KEY"
COMMAND_LOG = "command.log"
REQUEST_TIMEOUT_SECONDS = 180

sandbox_id_ctx_var = ContextVar(SANDBOX_ID, default="")

__all__ = [
    # System utilities
    "run_command_with_output",
    "run_shell_command",
    "extract_nohup_pid",
    "find_free_port",
    "release_port",
    "get_host_ip",
    "get_uniagent_endpoint",
    # Concurrent utilities
    "get_executor",
    "Timer",
    "RayUtil",
    "AsyncSafeDict",
    "AsyncAtomicInt",
    "run_until_complete",
    "timeout",
    # Data utilities
    "FileUtil",
    "ListUtil",
    # HTTP utilities
    "HttpUtils",
    "wait_until_alive",
    # Docker utilities
    "DockerUtil",
    "ImageUtil",
    # Importer utilities
    "can_import_class",
    "safe_import_class",
    # Exception handling
    "handle_exceptions",
    # Retry utilities
    "retry_async",
    # Constants
    "ENV_POOL",
    "sandbox_id_ctx_var",
    "SANDBOX_ID",
    "ROUTE_KEY",
    "COMMAND_LOG",
    "REQUEST_TIMEOUT_SECONDS",
]
