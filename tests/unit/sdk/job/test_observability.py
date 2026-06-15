"""Tests for rock.sdk.job.observability — reporter, decorator, env config."""

from __future__ import annotations

from rock import env_vars
from rock.sdk.job import observability
from rock.sdk.job.observability import JobMetricsReporter


class TestEnvVars:
    def test_otlp_endpoint_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        assert env_vars.ROCK_JOB_METRICS_OTLP_ENDPOINT is None

    def test_otlp_endpoint_reads_env(self, monkeypatch):
        monkeypatch.setenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", "http://otlp:4318/v1/metrics")
        assert env_vars.ROCK_JOB_METRICS_OTLP_ENDPOINT == "http://otlp:4318/v1/metrics"

    def test_high_cardinality_defaults_true(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS", raising=False)
        assert env_vars.ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS is True

    def test_high_cardinality_false_when_set_false(self, monkeypatch):
        monkeypatch.setenv("ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS", "false")
        assert env_vars.ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS is False


class _FakeCounter:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes):
        self.calls.append((value, attributes))


class TestReporter:
    def test_disabled_when_no_endpoint(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        assert reporter._enabled is False

    def test_record_exception_logs_when_disabled(self, monkeypatch, caplog):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        import logging

        # init_logger() sets propagate=False; caplog listens on the root logger,
        # so enable propagation for this assertion to capture the record.
        monkeypatch.setattr(observability.logger, "propagate", True)
        with caplog.at_level(logging.ERROR, logger="rock.sdk.job.observability"):
            reporter.record_exception("setup", "ValueError", "hard", {"job_name": "j1"})
        assert any("phase=setup" in r.message for r in caplog.records)
        assert any("severity=hard" in r.message for r in caplog.records)

    def test_record_exception_emits_counter_when_enabled(self, monkeypatch):
        reporter = JobMetricsReporter()
        reporter._enabled = True
        reporter._counter = _FakeCounter()
        reporter.record_exception("collect", "BashExitCode", "soft", {"job_name": "j1", "sandbox_id": "sb1"})
        assert len(reporter._counter.calls) == 1
        value, attrs = reporter._counter.calls[0]
        assert value == 1
        assert attrs["phase"] == "collect"
        assert attrs["severity"] == "soft"
        assert attrs["exception_type"] == "BashExitCode"
        assert attrs["job_name"] == "j1"
        assert attrs["sandbox_id"] == "sb1"

    def test_high_cardinality_labels_filtered_when_disabled(self, monkeypatch):
        monkeypatch.setenv("ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS", "false")
        reporter = JobMetricsReporter()
        reporter._enabled = True
        reporter._counter = _FakeCounter()
        reporter.record_exception("collect", "X", "soft", {"job_name": "j1", "sandbox_id": "sb1", "namespace": "ns"})
        _, attrs = reporter._counter.calls[0]
        assert "job_name" not in attrs
        assert "sandbox_id" not in attrs
        assert attrs["namespace"] == "ns"

    def test_get_reporter_is_singleton(self, monkeypatch):
        monkeypatch.setattr(observability, "_REPORTER", None)
        r1 = observability.get_reporter()
        r2 = observability.get_reporter()
        assert r1 is r2
