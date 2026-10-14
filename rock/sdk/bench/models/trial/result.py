"""Harbor trial result models."""

from __future__ import annotations

from typing import Any

from rock.sdk.job.result import ExceptionInfo
from rock.sdk.reward.result import AgentInfo, AgentResult, ModelInfo, RewardTrialResult, TimingInfo, VerifierResult

__all__ = [
    "AgentInfo",
    "AgentResult",
    "ExceptionInfo",
    "HarborTrialResult",
    "ModelInfo",
    "TimingInfo",
    "VerifierResult",
]


class HarborTrialResult(RewardTrialResult):
    """Harbor trial result backed by the shared reward protocol model."""

    @classmethod
    def from_harbor_json(cls, data: dict[str, Any]) -> HarborTrialResult:
        """Parse a harbor trial-level result.json dict into HarborTrialResult."""
        return cls.from_reward_json(data)
