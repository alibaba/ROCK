"""Append chat/completions trajectories as JSONL.

The recorder is invoked **explicitly** from ``proxy.py`` after each forwarded
call (success or failure). It is no longer a litellm CustomLogger — we removed
the litellm SDK dependency in favor of httpx-based byte forwarding, and call
this object directly so writes stay deterministic and locally testable.

Schema per line: a small dict with ``request`` / ``response`` / ``status`` /
``response_time`` / ``model`` / ``stream``. Faithful enough to drive the
sequential replayer; not a full StandardLoggingPayload.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from rock.logger import init_logger
from rock.sdk.model.server.utils import (
    MODEL_SERVICE_REQUEST_COUNT,
    MODEL_SERVICE_REQUEST_RT,
    _get_or_create_metrics_monitor,
)

logger = init_logger(__name__)


class TrajectoryRecorder:
    """Appends one JSONL line per chat/completions call and reports OTLP metrics."""

    def __init__(self, traj_file: str | os.PathLike) -> None:
        self.traj_file = Path(traj_file)
        self.traj_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._monitor = _get_or_create_metrics_monitor()

    async def record(
        self,
        *,
        request: dict[str, Any],
        response: dict[str, Any] | None,
        status: str,
        start_time: float,
        end_time: float,
        error: str | None = None,
    ) -> None:
        """Persist one call to the JSONL file and report RT/count metrics.

        ``request`` / ``response`` are stored verbatim (whatever the upstream
        returned, including provider-specific fields like ``reasoning_content``).
        For streaming calls, ``response`` is the aggregated final ChatCompletion
        produced by ``ChatCompletionStreamState.get_final_completion().model_dump()``.
        """
        rt_seconds = end_time - start_time
        payload = {
            "model": request.get("model"),
            "stream": bool(request.get("stream")),
            "status": status,
            "response_time": rt_seconds,
            "start_time": start_time,
            "end_time": end_time,
            "request": request,
            "response": response,
            "error": error,
        }

        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._write_line, line)

        attrs = {
            "type": "chat_completions",
            "status": status,
            "sandbox_id": os.getenv("ROCK_SANDBOX_ID", "unknown"),
        }
        self._monitor.record_gauge_by_name(MODEL_SERVICE_REQUEST_RT, rt_seconds * 1000.0, attributes=attrs)
        self._monitor.record_counter_by_name(MODEL_SERVICE_REQUEST_COUNT, 1, attributes=attrs)

    def _write_line(self, line: str) -> None:
        with self.traj_file.open("a", encoding="utf-8") as f:
            f.write(line)


def now() -> float:
    """Wall-clock seconds (single shim so callers don't import time directly)."""
    return time.time()
