"""Operator primitives that produce Trial lists from a JobConfig."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rock.sdk.job.trial.registry import _create_trial

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial


class Operator(ABC):
    """Operator base: apply(config) -> list[AbstractTrial]."""

    @abstractmethod
    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        """Generate a TrialList from config. Empty list means no-op."""
        ...


class ScatterOperator(Operator):
    """Create ``size`` identical Trial instances from config."""

    def __init__(self, size: int = 1):
        self.size = size

    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        if self.size <= 0:
            return []
        trial = _create_trial(config)
        return [trial] * self.size
