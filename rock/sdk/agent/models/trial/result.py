"""Trial result models aligned with harbor.models.trial.result."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ExceptionInfo(BaseModel):
    """Aligned with harbor.models.trial.result.ExceptionInfo"""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class ModelInfo(BaseModel):
    """Aligned with harbor.models.trial.result.ModelInfo"""

    name: str = ""
    provider: str = ""


class AgentInfo(BaseModel):
    """Aligned with harbor.models.trial.result.AgentInfo"""

    name: str = ""
    version: str = ""
    model_info: ModelInfo | None = None


class VerifierResult(BaseModel):
    """Aligned with harbor.models.verifier.result.VerifierResult"""

    rewards: dict[str, float | int] | None = None


class AgentResult(BaseModel):
    """Aligned with harbor.models.agent.context.AgentContext (subset)"""

    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    rollout_details: list[dict[str, Any]] | None = None


class TimingInfo(BaseModel):
    started_at: str | None = None
    finished_at: str | None = None


class TrialResult(BaseModel):
    """Aligned with harbor.models.trial.result.TrialResult"""

    task_name: str = ""
    trial_name: str = ""
    source: str | None = None
    agent_info: AgentInfo = Field(default_factory=AgentInfo)
    agent_result: AgentResult | None = None
    verifier_result: VerifierResult | None = None
    exception_info: ExceptionInfo | None = None
    started_at: str | None = None
    finished_at: str | None = None
    environment_setup: TimingInfo | None = None
    agent_setup: TimingInfo | None = None
    agent_execution: TimingInfo | None = None
    verifier: TimingInfo | None = None

    @property
    def score(self) -> float:
        if self.verifier_result and self.verifier_result.rewards:
            return self.verifier_result.rewards.get("reward", 0.0)
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

    @property
    def token_ids(self) -> list[int]:
        if self.agent_result and self.agent_result.rollout_details:
            ids = []
            for detail in self.agent_result.rollout_details:
                ids.extend(detail.get("completion_token_ids", []))
            return ids
        return []

    @classmethod
    def from_harbor_json(cls, data: dict[str, Any]) -> TrialResult:
        """Parse a harbor trial-level result.json dict into TrialResult."""
        exception_info = None
        if data.get("exception_info"):
            ei = data["exception_info"]
            if isinstance(ei, dict):
                exception_info = ExceptionInfo(**ei)
            else:
                exception_info = ExceptionInfo(exception_type="unknown", exception_message=str(ei))

        agent_info_data = data.get("agent_info") or {}
        model_info = None
        if agent_info_data.get("model_info"):
            model_info = ModelInfo(**agent_info_data["model_info"])
        agent_info = AgentInfo(
            name=agent_info_data.get("name", ""),
            version=agent_info_data.get("version", ""),
            model_info=model_info,
        )

        verifier_result = None
        if data.get("verifier_result"):
            verifier_result = VerifierResult(**data["verifier_result"])

        agent_result = None
        if data.get("agent_result"):
            agent_result = AgentResult(**data["agent_result"])

        return cls(
            task_name=data.get("task_name", ""),
            trial_name=data.get("trial_name", ""),
            source=data.get("source"),
            agent_info=agent_info,
            agent_result=agent_result,
            verifier_result=verifier_result,
            exception_info=exception_info,
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            environment_setup=TimingInfo(**data["environment_setup"]) if data.get("environment_setup") else None,
            agent_setup=TimingInfo(**data["agent_setup"]) if data.get("agent_setup") else None,
            agent_execution=TimingInfo(**data["agent_execution"]) if data.get("agent_execution") else None,
            verifier=TimingInfo(**data["verifier"]) if data.get("verifier") else None,
        )
