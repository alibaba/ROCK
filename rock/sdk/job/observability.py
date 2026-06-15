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

from rock import env_vars
from rock.logger import init_logger

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
            self._enabled = True
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


_REPORTER: JobMetricsReporter | None = None


def get_reporter() -> JobMetricsReporter:
    """Return the process-wide singleton reporter (lazy-init on first use)."""
    global _REPORTER
    if _REPORTER is None:
        _REPORTER = JobMetricsReporter()
    return _REPORTER
