"""Replay a recorded trajectory by registering a litellm CustomLLM provider.

Loads a single JSONL trajectory file on init, then hands records out one at a
time in recorded order. This is the simplest matching strategy and works for
deterministic agent runs that replay the same sequence of LLM calls
(SWE-agent / mini-swe-agent / OpenHands).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse
from litellm.utils import async_mock_completion_streaming_obj

from rock.logger import init_logger

logger = init_logger(__name__)


class SequentialCursor:
    """Hands out trajectory records one at a time, in recorded order.

    Going past the end raises CustomLLMError(404) so the proxy returns a clear
    error to the caller.
    """

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
                raise CustomLLMError(
                    status_code=404,
                    message=(f"trajectory exhausted at step {self._idx} (total recorded steps={len(self.records)})"),
                )
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


def _record_to_model_response(record: dict) -> ModelResponse:
    response = record.get("response")
    if not isinstance(response, dict):
        raise CustomLLMError(
            status_code=500,
            message=f"traj record at step has no usable 'response' dict: got {type(response).__name__}",
        )
    return ModelResponse(**response)


def _extract_assistant_text(record: dict) -> str:
    response = record.get("response") or {}
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


class TrajectoryReplayer(CustomLLM):
    """litellm CustomLLM that returns recorded responses in sequential order."""

    def __init__(self, traj_path: str | os.PathLike) -> None:
        super().__init__()
        self.cursor = SequentialCursor.load(traj_path)

    async def acompletion(
        self,
        model: str,
        messages: list,
        *args: Any,
        **kwargs: Any,
    ) -> ModelResponse:
        record = await self.cursor.next(expected_model=model)
        return _record_to_model_response(record)

    async def astreaming(
        self,
        model: str,
        messages: list,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        record = await self.cursor.next(expected_model=model)
        text = _extract_assistant_text(record)
        model_response = kwargs.get("model_response")
        async for chunk in async_mock_completion_streaming_obj(
            model_response=model_response,
            mock_response=text,
            model=model,
        ):
            yield chunk
