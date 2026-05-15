"""Trajectory record + replay for the chat/completions proxy.

Two halves around the same JSONL schema (one record per line):

- :class:`TrajectoryRecorder` — invoked by the forward path after each upstream
  call (success or failure). Appends a small dict with
  ``request`` / ``response`` / ``status`` / ``response_time`` / ``model`` /
  ``stream``, and reports OTLP RT/count metrics. Stores responses verbatim
  (provider-specific fields like ``reasoning_content`` survive); for streaming
  calls ``response`` is the aggregated final ChatCompletion produced by
  ``ChatCompletionStreamState.get_final_completion().model_dump()``.

- :class:`SequentialCursor` — loads a JSONL trajectory once at startup;
  ``await cursor.next(expected_model=...)`` hands out the next record (full
  payload dict) and advances. Going past the end raises
  :class:`TrajectoryExhausted` so the proxy can return a clean 404.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from rock.logger import init_logger
from rock.sdk.model.server.utils import (
    MODEL_SERVICE_REQUEST_COUNT,
    MODEL_SERVICE_REQUEST_RT,
    _get_or_create_metrics_monitor,
)

logger = init_logger(__name__)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Replay cursor
# ---------------------------------------------------------------------------


class TrajectoryExhausted(Exception):
    """Raised by ``SequentialCursor.next`` when all recorded steps have been served."""

    def __init__(self, position: int, total: int) -> None:
        super().__init__(f"trajectory exhausted at step {position} (total recorded steps={total})")
        self.position = position
        self.total = total


class SequentialCursor:
    """Hands out trajectory records one at a time, in recorded order."""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self._idx = 0
        self._lock = asyncio.Lock()

    @classmethod
    def load(cls, path: str | os.PathLike) -> SequentialCursor:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"traj file not found: {path}")

        records: list[dict] = []
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))

        logger.info(f"[traj-replay] loaded {len(records)} record(s) from {path}")
        return cls(records)

    async def next(self, expected_model: str | None = None) -> dict:
        async with self._lock:
            if self._idx >= len(self.records):
                raise TrajectoryExhausted(position=self._idx, total=len(self.records))
            record = self.records[self._idx]
            self._idx += 1
            current_idx = self._idx - 1

        if expected_model:
            recorded_model = record.get("model")
            if recorded_model and recorded_model != expected_model:
                logger.warning(
                    f"[traj-replay] step {current_idx} model mismatch: "
                    f"recorded={recorded_model!r} requested={expected_model!r}"
                )
        return record

    def reset(self) -> None:
        self._idx = 0

    @property
    def position(self) -> int:
        return self._idx

    @property
    def total(self) -> int:
        return len(self.records)
