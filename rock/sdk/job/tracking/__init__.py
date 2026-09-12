"""Tracking sub-package for the Job SDK.

Public API
----------
TrackingAdapter
    Abstract base class that third-party tracking backends implement.
resolve_tracking_adapters
    Runtime discovery of adapter classes from ``ROCK_TRACKING_LOAD_PATHS``.
"""

from rock.sdk.job.tracking.adapter import TrackingAdapter, resolve_tracking_adapters

__all__ = [
    "TrackingAdapter",
    "resolve_tracking_adapters",
]
