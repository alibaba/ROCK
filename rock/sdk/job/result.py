"""Result models for the Job system.

Base classes: TrialResult, RewardTrialResult, JobStatus, JobResult[T].
Harbor-specific subclasses live in rock.sdk.bench.models.trial.result.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# TrialResult base — common fields
# ---------------------------------------------------------------------------


class ExceptionInfo(BaseModel):
    """General exception info."""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class TrialResult(BaseModel):
    """Base class for a single execution result — common fields.

    Harbor's TrialResult inherits this class and adds agent_info, verifier_result, etc.
    Subclasses can override the score and status properties.
    """

    task_name: str = ""
    exception_info: ExceptionInfo | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # G5: process-level outputs captured by JobExecutor
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


class ModelInfo(BaseModel):
    name: str = ""
    provider: str = ""


class AgentInfo(BaseModel):
    name: str = ""
    version: str = ""
    model_info: ModelInfo | None = None


class VerifierResult(BaseModel):
    rewards: dict[str, float | int] | None = None


class AgentResult(BaseModel):
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    rollout_details: list[dict[str, Any]] | None = None


class TimingInfo(BaseModel):
    started_at: str | None = None
    finished_at: str | None = None


class RewardTrialResult(TrialResult):
    """Trial result parsed from the reward protocol's trial-level result.json."""

    trial_name: str = ""
    trial_uri: str | None = None
    task_id: dict[str, Any] | None = None
    source: str | None = None
    task_checksum: str | None = None
    config: dict[str, Any] | None = None
    agent_info: AgentInfo = Field(default_factory=AgentInfo)
    agent_result: AgentResult | None = None
    verifier_result: VerifierResult | None = None
    environment_setup: TimingInfo | None = None
    agent_setup: TimingInfo | None = None
    agent_execution: TimingInfo | None = None
    verifier: TimingInfo | None = None

    @property
    def score(self) -> float:
        if self.verifier_result and self.verifier_result.rewards:
            return float(self.verifier_result.rewards.get("reward", 0.0))
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
    def from_reward_json(cls, data: dict[str, Any]) -> RewardTrialResult:
        """Parse a reward-protocol trial-level result.json dict."""
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
            trial_uri=data.get("trial_uri"),
            task_id=data.get("task_id"),
            source=data.get("source"),
            task_checksum=data.get("task_checksum"),
            config=data.get("config"),
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


# ---------------------------------------------------------------------------
# JobStatus + JobResult[T]
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Job status enum."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


T = TypeVar("T", bound=TrialResult)


class JobResult(BaseModel, Generic[T]):
    """Aggregated result of a complete job run.

    Generic over trial result type T:
      - JobResult[TrialResult]       — base (new Job system)
      - JobResult[HarborTrialResult] — Harbor agent system
    """

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    labels: dict[str, str] = Field(default_factory=dict)
    trial_results: list[T] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trial_results:
            return 0.0
        return sum(t.score for t in self.trial_results) / len(self.trial_results)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "completed")

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == "failed")
