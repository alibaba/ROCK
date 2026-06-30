"""Tests for rock.sdk.job.adapter — TrackingAdapter base class + discovery."""

from __future__ import annotations

from rock.sdk.job.adapter import TrackingAdapter, resolve_tracking_adapter
from rock.sdk.job.config import JobConfig


class _ConcreteAdapter(TrackingAdapter):
    """Concrete adapter for testing (implements all abstract methods)."""

    def __init__(self):
        self.init_called = False
        self.received_config = None
        self.report_calls = []
        self.close_called = False

    def init(self, *, namespace, experiment_id, job_id, config):
        self.init_called = True
        self.received_config = config

    def report(self, metrics):
        self.report_calls.append(metrics)

    def close(self):
        self.close_called = True


class _FakeEntryPoint:
    """Fake entry_point that mimics importlib.metadata.EntryPoint."""

    def __init__(self, name, cls=None, error=None):
        self.name = name
        self._cls = cls
        self._error = error

    def load(self):
        if self._error:
            raise self._error
        return self._cls


class _FakeEntryPoints(list):
    """Fake entry_points() return value — a list that supports group kwarg."""

    pass


class TestResolveTrackingAdapter:
    def test_returns_none_when_no_entry_points(self, monkeypatch):
        monkeypatch.setattr(
            "rock.sdk.job.adapter.entry_points",
            lambda group=None: _FakeEntryPoints(),
        )
        result = resolve_tracking_adapter()
        assert result is None

    def test_loads_first_available_adapter(self, monkeypatch):
        monkeypatch.setattr(
            "rock.sdk.job.adapter.entry_points",
            lambda group=None: _FakeEntryPoints([_FakeEntryPoint("test_adapter", cls=_ConcreteAdapter)]),
        )
        result = resolve_tracking_adapter()
        assert isinstance(result, _ConcreteAdapter)

    def test_skips_broken_adapter(self, monkeypatch):
        monkeypatch.setattr(
            "rock.sdk.job.adapter.entry_points",
            lambda group=None: _FakeEntryPoints([_FakeEntryPoint("broken", error=ImportError("no module"))]),
        )
        result = resolve_tracking_adapter()
        assert result is None

    def test_adapter_lifecycle(self):
        adapter = _ConcreteAdapter()
        config = JobConfig(namespace="ns", experiment_id="exp", job_name="j1")
        adapter.init(namespace="ns", experiment_id="exp", job_id="j1", config=config)
        assert adapter.init_called
        assert adapter.received_config is config

        adapter.report({"score": 0.9, "status": "completed"})
        assert len(adapter.report_calls) == 1
        assert adapter.report_calls[0]["score"] == 0.9

        adapter.close()
        assert adapter.close_called
