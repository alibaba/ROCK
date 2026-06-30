"""Tests for rock.sdk.job.observability — reporter, decorator, env config."""

from __future__ import annotations

import pytest

from rock import env_vars
from rock.sdk.job import observability
from rock.sdk.job.observability import JobMetricsReporter, monitor_job_phase
from rock.sdk.job.result import ExceptionInfo, TrialResult


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


class _FakeProvider:
    def __init__(self, raises=False):
        self.shutdown_calls = 0
        self._raises = raises

    def shutdown(self):
        self.shutdown_calls += 1
        if self._raises:
            raise RuntimeError("provider shutdown blew up")


class TestShutdown:
    def test_shutdown_flushes_provider_and_disables(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        provider = _FakeProvider()
        reporter._provider = provider
        reporter._enabled = True
        reporter.shutdown()
        assert provider.shutdown_calls == 1
        assert reporter._enabled is False

    def test_shutdown_is_idempotent(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        provider = _FakeProvider()
        reporter._provider = provider
        reporter.shutdown()
        reporter.shutdown()
        assert provider.shutdown_calls == 1

    def test_shutdown_swallows_provider_errors(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        reporter._provider = _FakeProvider(raises=True)
        # must never raise — observability cannot break process teardown
        reporter.shutdown()
        assert reporter._enabled is False

    def test_shutdown_noop_without_provider(self, monkeypatch):
        monkeypatch.delenv("ROCK_JOB_METRICS_OTLP_ENDPOINT", raising=False)
        reporter = JobMetricsReporter()
        assert reporter._provider is None
        reporter.shutdown()  # no provider -> clean no-op


class _FakeReporter:
    def __init__(self):
        self.events = []  # (phase, exc_type, severity, labels)

    def record_exception(self, phase, exc_type, severity, labels):
        self.events.append((phase, exc_type, severity, labels))


class _FakeTrial:
    """Duck-typed AbstractTrial: only needs ._config."""

    def __init__(self, config):
        self._config = config


class _Cfg:
    def __init__(self, job_name="j", experiment_id="e", namespace="n"):
        self.job_name = job_name
        self.experiment_id = experiment_id
        self.namespace = namespace


class _Holder:
    """Stand-in for JobExecutor — hosts decorated methods."""

    @monitor_job_phase("setup")
    async def boom_async(self, trial):
        raise ValueError("nope")

    @monitor_job_phase("collect")
    async def soft_async(self, trial):
        return TrialResult(task_name="t", exception_info=ExceptionInfo(exception_type="BashExitCode"))

    @monitor_job_phase("collect")
    async def ok_async(self, trial):
        return TrialResult(task_name="t")

    @monitor_job_phase("launch")
    def boom_sync(self, trial):
        raise RuntimeError("sync-nope")


class TestDecorator:
    async def test_hard_fail_emits_once_and_reraises(self, monkeypatch):
        fake = _FakeReporter()
        monkeypatch.setattr(observability, "_REPORTER", fake)
        h = _Holder()
        with pytest.raises(ValueError, match="nope"):
            await h.boom_async(_FakeTrial(_Cfg(job_name="j1")))
        assert len(fake.events) == 1
        phase, exc_type, severity, labels = fake.events[0]
        assert phase == "setup"
        assert exc_type == "ValueError"
        assert severity == "hard"
        assert labels["job_name"] == "j1"
        assert labels["trial_type"] == "fake"  # _FakeTrial -> "fake"

    async def test_soft_fail_emitted_from_return_value(self, monkeypatch):
        fake = _FakeReporter()
        monkeypatch.setattr(observability, "_REPORTER", fake)
        h = _Holder()
        await h.soft_async(_FakeTrial(_Cfg()))
        assert len(fake.events) == 1
        phase, exc_type, severity, _ = fake.events[0]
        assert phase == "collect"
        assert exc_type == "BashExitCode"
        assert severity == "soft"

    async def test_clean_return_emits_nothing(self, monkeypatch):
        fake = _FakeReporter()
        monkeypatch.setattr(observability, "_REPORTER", fake)
        h = _Holder()
        await h.ok_async(_FakeTrial(_Cfg()))
        assert fake.events == []

    def test_sync_hard_fail_emits(self, monkeypatch):
        fake = _FakeReporter()
        monkeypatch.setattr(observability, "_REPORTER", fake)
        h = _Holder()
        with pytest.raises(RuntimeError, match="sync-nope"):
            h.boom_sync(_FakeTrial(_Cfg()))
        assert len(fake.events) == 1
        assert fake.events[0][0] == "launch"
        assert fake.events[0][2] == "hard"

    async def test_broken_reporter_does_not_break_job(self, monkeypatch):
        class _ExplodingReporter:
            def record_exception(self, *a, **k):
                raise RuntimeError("reporter blew up")

        monkeypatch.setattr(observability, "_REPORTER", _ExplodingReporter())
        h = _Holder()
        # soft path: a broken reporter must NOT propagate out of the phase
        result = await h.soft_async(_FakeTrial(_Cfg()))
        assert result.exception_info is not None  # job result still returned

        # hard path: the ORIGINAL exception must still propagate (not the reporter's)
        with pytest.raises(ValueError, match="nope"):
            await h.boom_async(_FakeTrial(_Cfg()))
