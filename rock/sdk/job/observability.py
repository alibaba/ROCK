"""Client-side exception observability for the SDK job module.

Provides a lazily-initialized singleton ``JobMetricsReporter`` (opt-in OTLP
counter, log-only when no endpoint is configured) and the ``monitor_job_phase``
decorator used by JobExecutor to emit one counter per exception with the
failing phase attributed.

Metric: ``rock_job.exception.total`` (counter).
Labels: phase, severity, exception_type, trial_type, job_name, experiment_id,
namespace, sandbox_id. job_name/sandbox_id are dropped from the metric (not the
log) when ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS is false.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
from collections.abc import Callable

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.job.result import TrialResult

logger = init_logger(__name__)

_COUNTER_NAME = "rock_job.exception.total"
_HIGH_CARDINALITY_KEYS = ("job_name", "sandbox_id")


def _fmt_labels(labels: dict[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in labels.items())


class JobMetricsReporter:
    """Dual-writes job exceptions to logs + (optionally) an OTLP counter."""

    def __init__(self) -> None:
        self._enabled = False
        self._counter = None
        self._reader = None
        self._provider = None
        endpoint = env_vars.ROCK_JOB_METRICS_OTLP_ENDPOINT
        if endpoint:
            self._init_otel(endpoint)

    def _init_otel(self, endpoint: str) -> None:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
            provider = MeterProvider(metric_readers=[reader])
            meter = provider.get_meter("rock.sdk.job")
            self._counter = meter.create_counter(
                name=_COUNTER_NAME,
                description="Count of job execution exceptions (hard + soft)",
                unit="1",
            )
            self._reader = reader
            self._provider = provider
            self._enabled = True
            atexit.register(self.shutdown)
            logger.info("job metrics reporter enabled endpoint=%s", endpoint)
        except Exception as e:  # noqa: BLE001 — never let metrics setup break jobs
            logger.warning("job metrics reporter init failed, log-only mode: %s", e)
            self._enabled = False
            self._counter = None

    def _filter_labels(self, labels: dict[str, str]) -> dict[str, str]:
        if env_vars.ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS:
            return labels
        return {k: v for k, v in labels.items() if k not in _HIGH_CARDINALITY_KEYS}

    def record_exception(self, phase: str, exc_type: str, severity: str, labels: dict[str, str]) -> None:
        # 1) structured log — always, always with full labels
        logger.error(
            "job exception phase=%s severity=%s exception_type=%s %s",
            phase,
            severity,
            exc_type,
            _fmt_labels(labels),
        )
        # 2) metric — only when enabled; high-card filter happens here, not on the log
        if self._enabled and self._counter is not None:
            attrs = {
                "phase": phase,
                "severity": severity,
                "exception_type": exc_type,
                **self._filter_labels(labels),
            }
            self._counter.add(1, attrs)

    def shutdown(self) -> None:
        """Flush buffered metrics before the process exits.

        PeriodicExportingMetricReader exports on an interval, so a short-lived
        job can die with its last exceptions still buffered. Registered via
        atexit so the provider force-flushes on a clean interpreter exit.
        Idempotent and exception-isolated: teardown must never raise.
        """
        provider = self._provider
        if provider is None:
            return
        self._provider = None
        self._enabled = False
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 — never let metrics teardown break exit
            logger.warning("job metrics reporter shutdown failed", exc_info=True)


_REPORTER: JobMetricsReporter | None = None


def get_reporter() -> JobMetricsReporter:
    """Return the process-wide singleton reporter (lazy-init on first use)."""
    global _REPORTER
    if _REPORTER is None:
        _REPORTER = JobMetricsReporter()
    return _REPORTER


def _trial_type(trial) -> str:
    name = type(trial).__name__
    base = name[:-5] if name.endswith("Trial") else name
    return base.lower().lstrip("_")


def _extract_labels(args: tuple) -> dict[str, str]:
    """Best-effort label extraction from a decorated method's positional args.

    Duck-typed (no imports of AbstractTrial/TrialClient/Sandbox) to avoid
    circular imports:
      - TrialClient : has both ``.trial`` and ``.sandbox``
      - AbstractTrial: has ``._config``
      - Sandbox     : has ``.sandbox_id``
    """
    trial = None
    sandbox = None
    for a in args:
        if hasattr(a, "trial") and hasattr(a, "sandbox"):
            trial = a.trial
            sandbox = a.sandbox
        elif hasattr(a, "_config"):
            trial = a
        elif hasattr(a, "sandbox_id"):
            sandbox = a
        # Stop once we have a full (trial, sandbox) pair so later positional
        # args (e.g. the nohup observation) can't clobber them.
        if trial is not None and sandbox is not None:
            break

    labels = {
        "trial_type": "unknown",
        "job_name": "unknown",
        "experiment_id": "unknown",
        "namespace": "unknown",
        "sandbox_id": "unknown",
    }
    if trial is not None:
        cfg = trial._config
        labels["trial_type"] = _trial_type(trial)
        labels["job_name"] = str(getattr(cfg, "job_name", None) or "unknown")
        labels["experiment_id"] = str(getattr(cfg, "experiment_id", None) or "unknown")
        labels["namespace"] = str(getattr(cfg, "namespace", None) or "unknown")
    if sandbox is not None:
        labels["sandbox_id"] = str(getattr(sandbox, "sandbox_id", None) or "unknown")
    return labels


def _emit_soft(reporter, phase: str, result, args: tuple) -> None:
    """Emit one soft event per returned TrialResult carrying exception_info.

    Labels are extracted lazily, only when a soft fail is actually present, so
    the success path pays no extraction cost.
    """
    items = result if isinstance(result, list) else [result]
    labels = None
    for r in items:
        if isinstance(r, TrialResult) and r.exception_info is not None:
            if labels is None:
                labels = _extract_labels(args)
            reporter.record_exception(
                phase=phase,
                exc_type=r.exception_info.exception_type or "unknown",
                severity="soft",
                labels=labels,
            )


def _safe_record_hard(reporter, phase: str, exc: BaseException, args: tuple) -> None:
    """Record a hard failure. Observability must never break a job, so any
    error in extraction/recording is swallowed and logged."""
    try:
        reporter.record_exception(phase, type(exc).__name__, "hard", _extract_labels(args))
    except Exception:
        logger.warning("job observability failed recording hard exception for phase=%s", phase, exc_info=True)


def _safe_emit_soft(reporter, phase: str, result, args: tuple) -> None:
    """Emit soft failures. Swallows and logs any observability error so it can
    never break a job."""
    try:
        _emit_soft(reporter, phase, result, args)
    except Exception:
        logger.warning("job observability failed emitting soft exception for phase=%s", phase, exc_info=True)


def monitor_job_phase(phase: str) -> Callable:
    """Decorate a JobExecutor phase method to emit log+metric on exceptions.

    - Hard fail (method raises): record severity="hard" then re-raise.
    - Soft fail (method returns TrialResult(s) carrying exception_info):
      record severity="soft" for each.
    Only the leaf phase methods are decorated; outer _do_submit/_do_wait are
    NOT decorated, so a re-raised hard fail is counted exactly once.

    All observability side-effects are exception-isolated: a bug in label
    extraction or recording can never turn a successful phase into a failure,
    and the original wrapped exception is always re-raised unchanged.
    """

    def deco(f):
        if asyncio.iscoroutinefunction(f):

            @functools.wraps(f)
            async def awrapper(self, *args, **kwargs):
                reporter = get_reporter()
                try:
                    result = await f(self, *args, **kwargs)
                except Exception as e:
                    _safe_record_hard(reporter, phase, e, args)
                    raise
                _safe_emit_soft(reporter, phase, result, args)
                return result

            return awrapper

        @functools.wraps(f)
        def wrapper(self, *args, **kwargs):
            reporter = get_reporter()
            try:
                result = f(self, *args, **kwargs)
            except Exception as e:
                _safe_record_hard(reporter, phase, e, args)
                raise
            _safe_emit_soft(reporter, phase, result, args)
            return result

        return wrapper

    return deco
