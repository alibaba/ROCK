"""Pluggable tracking adapter protocol for Job SDK.

Adapters are discovered by scanning the directories listed in the
``ROCK_TRACKING_LOAD_PATHS`` environment variable (comma-separated) for
``TrackingAdapter`` subclasses — the same directory-scan pattern used by the
CLI CommandLoader.

Internal / proprietary adapters (e.g. one that reports to an internal backend)
are layered into the default directory (``rock/sdk/tracking``) via a symlink,
so no entry_points registration or ``pip install`` step is required. When the
directory is empty or absent (as in the published open-source package),
``resolve_tracking_adapters()`` simply returns an empty list.
"""

from __future__ import annotations

import abc
import importlib.util
import inspect
import os
from typing import TYPE_CHECKING, Any

from rock import env_vars
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig

logger = init_logger(__name__)


class TrackingAdapter(abc.ABC):
    """Adapter protocol for job metrics reporting.

    Lifecycle: init() → report() × N → close()

    Implementations decide what backend to write to (OTel, custom backends, etc.)
    and how to translate the neutral metrics dict into their wire format.
    """

    @abc.abstractmethod
    def init(self, *, namespace: str, experiment_id: str, job_id: str, config: JobConfig) -> None:
        """Initialize a tracking session.

        Args:
            namespace:     Tracking namespace (e.g. OTel service name).
            experiment_id: Experiment identifier.
            job_id:        Job / run identifier. Typically ``config.job_name``.
            config:        Full JobConfig instance. Adapter extracts what it needs
                          (e.g. model_name from agents, labels, environment.env).
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


def _load_adapter_classes(directory: str) -> list[type[TrackingAdapter]]:
    """Scan one directory for TrackingAdapter subclasses.

    Imports every ``.py`` file (except ``__init__.py``) and collects concrete
    ``TrackingAdapter`` subclasses defined in it. A file that fails to import is
    logged and skipped so one broken adapter cannot break job execution.
    """
    classes: list[type[TrackingAdapter]] = []
    for root, _dirs, files in os.walk(directory):
        for file in files:
            if file == "__init__.py" or not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file)
            module_name = os.path.relpath(filepath, directory).replace(os.sep, ".").removesuffix(".py")

            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as e:  # noqa: BLE001 — a broken adapter must not break jobs
                logger.warning("failed loading tracking adapter file %s: %s", filepath, e)
                continue

            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, TrackingAdapter) and obj is not TrackingAdapter and obj.__module__ == module_name:
                    classes.append(obj)
    return classes


def resolve_tracking_adapters() -> list[TrackingAdapter]:
    """Discover all tracking adapters by scanning ROCK_TRACKING_LOAD_PATHS.

    Returns a list of instantiated adapters, one per discovered
    ``TrackingAdapter`` subclass. Returns an empty list if no directory contains
    an adapter or all fail to load / instantiate.
    """
    adapters: list[TrackingAdapter] = []
    for directory in env_vars.ROCK_TRACKING_LOAD_PATHS.split(","):
        directory = directory.strip()
        if not directory:
            continue
        for cls in _load_adapter_classes(directory):
            try:
                adapter = cls()
                logger.info("tracking adapter loaded: %s", cls.__name__)
                adapters.append(adapter)
            except Exception as e:  # noqa: BLE001 — adapter failure must not break jobs
                logger.warning("failed instantiating tracking adapter %s: %s", cls.__name__, e)
    return adapters
