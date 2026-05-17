"""Sentinel file read/write helpers for deferred sandbox log archival.

The sentinel is a small JSON file dropped into a stopped sandbox's log
directory by DockerDeployment._stop(); SandboxLogArchiveTask later picks
up dirs whose sentinel is older than `oss.keep_days_before_archive` and
ships them to OSS.

Sentinel JSON shape (versioned for future evolution):
    {
        "version": 1,
        "stopped_at": "2026-05-13T10:30:00+08:00",  // ISO 8601, tz-aware
        "attempts": 0                                // archive retry count
    }
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from rock.logger import init_logger

logger = init_logger(__name__)

LOG_STOPPED_SENTINEL = ".rock_stopped_at"
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


def dump_state(state: SentinelState) -> str:
    """Serialize a SentinelState to its on-disk JSON form.

    Single source of truth for the schema — admin-side code that needs to
    overwrite a remote worker's sentinel via runtime.write_file() reuses
    this so the JSON layout stays consistent with local writes.
    """
    return json.dumps(asdict(state), ensure_ascii=False)


def _atomic_write(target: Path, content: str) -> None:
    # tempfile + rename inside the same directory: rename is POSIX atomic,
    # so a crashed write never leaves a half-written sentinel on disk.
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_name, target)
    except Exception as e:
        logger.warning(f"[sentinel] _atomic_write failed for {target}: {e}", exc_info=True)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_sentinel(log_dir: Path, state: SentinelState | None = None) -> None:
    target = sentinel_path(log_dir)
    if state is None:
        state = SentinelState.now()
    _atomic_write(target, dump_state(state))


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
