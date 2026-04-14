"""Operator — generic algorithm that produces a TrialList from a JobConfig."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rock.sdk.job.trial.registry import _create_trial

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial


class Operator(ABC):
    """Operator base: apply(config) -> list[AbstractTrial].

    Operators generate a TrialList from a config. They don't manage
    sandbox lifecycle (JobExecutor does) — just decide what to run.
    """

    @abstractmethod
    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        """Generate a TrialList from config. Empty list means no-op."""
        ...


class ScatterOperator(Operator):
    """Scatter: create `size` identical Trial instances from config.

    Analog of torch.distributed.scatter — same data/config distributed to N workers.

    Usage:
      ScatterOperator()           # size=1, single trial (default)
      ScatterOperator(size=8)     # 8 parallel trials
      ScatterOperator(size=0)     # empty list, no-op
    """

    def __init__(self, size: int = 1):
        self.size = size

    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        if self.size <= 0:
            return []
        trial = _create_trial(config)
        return [trial] * self.size
