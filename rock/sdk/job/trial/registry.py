"""Trial registry — maps JobConfig subclasses to their AbstractTrial implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial

_TRIAL_REGISTRY: dict[type[JobConfig], type[AbstractTrial]] = {}


def register_trial(config_type: type[JobConfig], trial_type: type[AbstractTrial]) -> None:
    """Register a Config → Trial mapping."""
    _TRIAL_REGISTRY[config_type] = trial_type


def _create_trial(config: JobConfig) -> AbstractTrial:
    """Create a Trial instance for the given config.

    Raises TypeError if no trial class has been registered for this config type.
    """
    trial_cls = _TRIAL_REGISTRY.get(type(config))
    if trial_cls is None:
        raise TypeError(
            f"No trial registered for {type(config).__name__}. Supported: {[c.__name__ for c in _TRIAL_REGISTRY]}"
        )
    return trial_cls(config)
