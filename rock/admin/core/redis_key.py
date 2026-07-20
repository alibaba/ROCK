ALIVE_PREFIX = "alive:"
TIMEOUT_PREFIX = "timeout:"
OPENSANDBOX_SESSIONS_PREFIX = "opensandbox:sessions:"


def alive_sandbox_key(sandbox_id: str) -> str:
    return f"{ALIVE_PREFIX}{sandbox_id}"


def timeout_sandbox_key(sandbox_id: str) -> str:
    return f"{TIMEOUT_PREFIX}{sandbox_id}"


def opensandbox_sessions_key(sandbox_id: str) -> str:
    return f"{OPENSANDBOX_SESSIONS_PREFIX}{sandbox_id}"
