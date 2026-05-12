"""Sequential cursor over a recorded JSONL trajectory.

Loaded once at startup; ``await cursor.next(expected_model=...)`` hands out the
next record (full StandardLoggingPayload dict) and advances. Going past the end
raises :class:`TrajectoryExhausted` so the proxy can return a clean 404 without
involving litellm — that's the whole point: replay does NOT need to go through
litellm's CustomLLM machinery, the proxy serves recorded responses directly.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from rock.logger import init_logger

logger = init_logger(__name__)


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
