"""Pluggable tracking adapter protocol for Job SDK.

Third-party packages register adapters via entry_points:

    [project.entry-points."rock.job.tracking_adapter"]
    my_backend = "my_package.tracking:MyAdapter"

The ``resolve_tracking_adapter()`` function discovers and instantiates
the first available adapter.
"""

from __future__ import annotations

import abc
from importlib.metadata import entry_points
from typing import Any

from rock.logger import init_logger

logger = init_logger(__name__)

_ENTRY_POINT_GROUP = "rock.job.tracking_adapter"


class TrackingAdapter(abc.ABC):
    """Adapter protocol for job metrics reporting.

    Lifecycle: init() → report() × N → close()

    Implementations decide what backend to write to (OTel, custom backends, etc.)
    and how to translate the neutral metrics dict into their wire format.
    """

    @abc.abstractmethod
    def init(self, *, namespace: str, experiment_id: str, job_id: str, config: dict[str, Any]) -> None:
        """Initialize a tracking session.

        Args:
            project:  Tracking project name / OTel service name.
                      Typically ``config.namespace`` or ``config.experiment_id``.
            run_name: Display name for this run. Typically ``config.job_name``.
            config:   Flat dict of job-level metadata (labels, hyperparams, etc.).
        """

    @abc.abstractmethod
    def report(self, metrics: dict[str, Any]) -> None:
        """Report one set of metrics.

        The metrics dict is intentionally flat and backend-agnostic.
        Adapters translate it into their own wire format.
        """

    def close(self) -> None:
        """Flush and close the tracking session.

        Must be idempotent. Must never raise.
        Default implementation is a no-op.
        """


def resolve_tracking_adapter() -> TrackingAdapter | None:
    """Discover a tracking adapter via entry_points.

    Returns the first successfully loaded adapter, or None if no
    entry_points are registered or all fail to load.
    """
    eps = entry_points(group=_ENTRY_POINT_GROUP)
    for ep in eps:
        try:
            cls = ep.load()
            adapter = cls()
            logger.info("tracking adapter loaded: %s (%s)", ep.name, type(adapter).__name__)
            return adapter
        except Exception as e:  # noqa: BLE001 — adapter failure must not break jobs
            logger.warning("failed loading tracking adapter %s: %s", ep.name, e)
    return None
