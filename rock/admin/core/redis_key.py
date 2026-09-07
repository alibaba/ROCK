ALIVE_PREFIX = "alive:"
TIMEOUT_PREFIX = "timeout:"
OPENSANDBOX_SESSIONS_PREFIX = "opensandbox:sessions:"
DADI_IMAGE_CACHE_PREFIX = "e2b:dadi:exists:v1:"


def alive_sandbox_key(sandbox_id: str) -> str:
    return f"{ALIVE_PREFIX}{sandbox_id}"


def timeout_sandbox_key(sandbox_id: str) -> str:
    return f"{TIMEOUT_PREFIX}{sandbox_id}"


def opensandbox_sessions_key(sandbox_id: str) -> str:
    return f"{OPENSANDBOX_SESSIONS_PREFIX}{sandbox_id}"


def dadi_image_cache_key(probe_registry: str, mapped_image: str) -> str:
    return f"{DADI_IMAGE_CACHE_PREFIX}{probe_registry}:{mapped_image}"
