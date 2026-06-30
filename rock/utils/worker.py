"""Pure helpers for multi-worker proxy sizing. No I/O, fully unit-testable."""

from __future__ import annotations

MIN_POOL_SIZE = 2

# Envs that fall back to fakeredis / in-memory sqlite (per-process state).
# Multi-worker there would unshare sandbox state across workers, so force 1.
SINGLE_WORKER_ENVS = frozenset({"local", "test", "dev"})


def resolve_workers(role: str, override: int | None, env_workers: int, env: str | None = None) -> int:
    """Resolve uvicorn worker count.

    admin role is always single-process (owns scheduler/Ray singletons).
    local/test/dev are always single-process (fakeredis/in-memory state is
    per-process; multi-worker would unshare it) — this overrides override/env.
    proxy role otherwise: explicit override > env > 1. Worker count must be set
    explicitly (via --workers or ROCK_PROXY_WORKERS); no cpu_count auto-detect.
    """
    if role != "proxy":
        return 1
    if env in SINGLE_WORKER_ENVS:
        return 1
    if override and override > 0:
        return override
    if env_workers and env_workers > 0:
        return env_workers
    return 1
