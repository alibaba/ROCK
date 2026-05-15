"""Sentinel file read/write helper for KEEP_THEN_ARCHIVE deferred archival.

Sentinel JSON shape (versioned for future evolution):
    {
        "version": 1,
        "stopped_at": "2026-05-13T10:30:00+08:00",  // ISO 8601, tz-aware
        "attempts": 0                                // archive retry count
    }
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from rock.deployments.log_cleanup import LOG_STOPPED_SENTINEL
from rock.logger import init_logger

logger = init_logger(__name__)

_SENTINEL_VERSION = 1


@dataclass
class SentinelState:
    stopped_at: str
    attempts: int = 0
    version: int = _SENTINEL_VERSION

    @classmethod
    def now(cls) -> "SentinelState":
        return cls(stopped_at=datetime.now().astimezone().isoformat(), attempts=0)


def sentinel_path(log_dir: Path) -> Path:
    return log_dir / LOG_STOPPED_SENTINEL


def write_sentinel(log_dir: Path, state: SentinelState | None = None) -> None:
    """Idempotent: if sentinel already exists, do not overwrite stopped_at;
    only used at _stop() and at attempts bump."""
    target = sentinel_path(log_dir)
    if state is None:
        state = SentinelState.now()
    target.write_text(json.dumps(asdict(state), ensure_ascii=False))


def read_sentinel(log_dir: Path) -> SentinelState | None:
    """Returns None when sentinel missing or unreadable. Caller treats
    None as "not yet stopped" (i.e. skip archive)."""
    target = sentinel_path(log_dir)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text())
        return SentinelState(
            stopped_at=data["stopped_at"],
            attempts=int(data.get("attempts", 0)),
            version=int(data.get("version", _SENTINEL_VERSION)),
        )
    except Exception as e:
        logger.warning(f"sentinel read failed for {target}: {e}")
        return None


def bump_attempts(log_dir: Path) -> int:
    """Increment attempts on the sentinel; returns new value.
    If sentinel is missing, recreate with attempts=1 (best-effort)."""
    state = read_sentinel(log_dir) or SentinelState.now()
    state.attempts += 1
    write_sentinel(log_dir, state)
    return state.attempts
