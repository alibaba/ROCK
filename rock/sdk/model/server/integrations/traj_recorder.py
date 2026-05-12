"""Record chat/completions trajectories as JSONL via litellm's CustomLogger hook.

One line per call, each line is a ``StandardLoggingPayload`` dict from litellm.
Streaming chunks are aggregated by litellm before this callback fires (see
litellm/litellm_core_utils/litellm_logging.py around line 1930), so we don't
need to handle the streaming/non-streaming split ourselves.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

from rock.logger import init_logger
from rock.sdk.model.server.utils import (
    MODEL_SERVICE_REQUEST_COUNT,
    MODEL_SERVICE_REQUEST_RT,
    _get_or_create_metrics_monitor,
)

logger = init_logger(__name__)


class TrajectoryRecorder(CustomLogger):
    """litellm CustomLogger that appends each call's StandardLoggingPayload to JSONL
    and reports OTLP RT/count metrics."""

    def __init__(self, traj_file: str | os.PathLike) -> None:
        super().__init__()
        self.traj_file = Path(traj_file)
        self.traj_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._monitor = _get_or_create_metrics_monitor()

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        payload = kwargs.get("standard_logging_object")
        if payload is None:
            logger.debug("[traj-recorder] success event without standard_logging_object, skipping")
            return
        await self._append_jsonl(payload)
        self._record_metrics(payload, status="success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        payload = kwargs.get("standard_logging_object")
        if payload is None:
            return
        await self._append_jsonl(payload)
        self._record_metrics(payload, status="failure")

    async def _append_jsonl(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        with self.traj_file.open("a", encoding="utf-8") as f:
            f.write(line)

    def _record_metrics(self, payload: dict, *, status: str) -> None:
        rt_seconds = payload.get("response_time")
        if rt_seconds is None:
            start = payload.get("startTime")
            end = payload.get("endTime")
            rt_seconds = (end - start) if (start is not None and end is not None) else 0.0
        rt_ms = float(rt_seconds) * 1000.0

        attrs = {
            "type": "chat_completions",
            "status": status,
            "sandbox_id": os.getenv("ROCK_SANDBOX_ID", "unknown"),
        }
        self._monitor.record_gauge_by_name(MODEL_SERVICE_REQUEST_RT, rt_ms, attributes=attrs)
        self._monitor.record_counter_by_name(MODEL_SERVICE_REQUEST_COUNT, 1, attributes=attrs)
