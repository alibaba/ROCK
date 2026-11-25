"""Shared SDK result base models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ExceptionInfo(BaseModel):
    """General exception info."""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class TrialResult(BaseModel):
    """Base class for a single execution result."""

    task_name: str = ""
    exception_info: ExceptionInfo | None = None
    started_at: str | None = None
    finished_at: str | None = None
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        return 0.0

    @property
    def status(self) -> str:
        return "failed" if self.exception_info else "completed"

    @property
    def duration_sec(self) -> float:
        if self.started_at and self.finished_at:
            try:
                start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
                return (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        return 0.0
