"""Local training artifacts shared by JsonLogger and iteration timing."""

from __future__ import annotations

import json
from typing import Any

from .storage import LocalStorage


class TrainingRunStore:
    def __init__(self, storage: LocalStorage) -> None:
        self.storage = storage

    def write_config(self, config: Any) -> None:
        self.storage.write_text(
            "config.json", json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        )

    def write_code_diff(self, diff: str) -> None:
        self.storage.write_text("code.diff", diff)

    def write_metrics(self, metrics: dict[str, Any], step: int | None = None) -> None:
        record = dict(metrics)
        if step is not None:
            record["step"] = step
        self.storage.append_text("metrics.jsonl", json.dumps(record, ensure_ascii=False) + "\n")

    def write_timing_spans(self, step: int, spans: list[dict[str, Any]]) -> None:
        if spans:
            record = {"step": step, "spans": spans}
            self.storage.append_text(
                "timing_spans.jsonl", json.dumps(record, ensure_ascii=False) + "\n"
            )
