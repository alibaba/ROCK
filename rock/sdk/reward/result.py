"""Shared reward protocol result models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from rock.sdk.result import ExceptionInfo, TrialResult


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

    @field_validator("exception_info", mode="before")
    @classmethod
    def _coerce_exception_info(cls, value: Any) -> Any:
        if not value or isinstance(value, ExceptionInfo) or isinstance(value, dict):
            return value
        return {
            "exception_type": "unknown",
            "exception_message": str(value),
        }

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
        return cls.model_validate(data)
