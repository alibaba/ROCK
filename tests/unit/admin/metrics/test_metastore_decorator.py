"""Tests for monitor_metastore_operation decorator."""

from unittest.mock import Mock

import pytest

from rock.admin.metrics.decorator import monitor_metastore_operation
from rock.admin.metrics.monitor import MetricsMonitor


def _make_monitor(prefix="meta_store"):
    monitor = Mock(spec=MetricsMonitor)
    monitor._should_skip.return_value = False
    monitor.metric_prefix = prefix
    return monitor


class FakeStore:
    """Minimal store-like object for testing the decorator."""

    def __init__(self, metrics_monitor=None):
        self.metrics_monitor = metrics_monitor

    @monitor_metastore_operation
    async def do_something(self):
        return "ok"

    @monitor_metastore_operation
    async def do_fail(self):
        raise ValueError("boom")

    @monitor_metastore_operation
    async def get(self, sandbox_id: str):
        return sandbox_id

    @monitor_metastore_operation
    async def list_by(self, field: str, value: str):
        return []


class TestMonitorMetastoreOperation:
    async def test_records_success_metrics(self):
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        result = await store.do_something()

        assert result == "ok"
        attrs = {"operation": "do_something", "method": "do_something", "sandbox_id": "unknown"}
        monitor.record_counter_by_name.assert_any_call("meta_store.success", 1, attrs)
        monitor.record_counter_by_name.assert_any_call("meta_store.total", 1, attrs)
        monitor.record_gauge_by_name.assert_called_once()
        call_args = monitor.record_gauge_by_name.call_args
        assert call_args[0][0] == "meta_store.rt"
        assert call_args[0][2] == attrs

    async def test_records_failure_metrics(self):
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        with pytest.raises(ValueError, match="boom"):
            await store.do_fail()

        error_attrs = {"operation": "do_fail", "method": "do_fail", "sandbox_id": "unknown", "error_type": "ValueError"}
        monitor.record_counter_by_name.assert_any_call("meta_store.failure", 1, error_attrs)
        monitor.record_counter_by_name.assert_any_call("meta_store.total", 1, error_attrs)
        monitor.record_gauge_by_name.assert_called_once()

    async def test_skips_when_no_monitor(self):
        store = FakeStore(metrics_monitor=None)

        result = await store.do_something()

        assert result == "ok"

    async def test_rt_is_positive(self):
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        await store.do_something()

        call_args = monitor.record_gauge_by_name.call_args
        rt_value = call_args[0][1]
        assert rt_value > 0

    async def test_uses_monitor_prefix(self):
        """Verify the decorator reads metric_prefix from the monitor instance."""
        monitor = _make_monitor(prefix="meta_store.db")
        store = FakeStore(metrics_monitor=monitor)

        await store.do_something()

        attrs_db = {"operation": "do_something", "method": "do_something", "sandbox_id": "unknown"}
        monitor.record_counter_by_name.assert_any_call("meta_store.db.success", 1, attrs_db)
        monitor.record_counter_by_name.assert_any_call("meta_store.db.total", 1, attrs_db)
        call_args = monitor.record_gauge_by_name.call_args
        assert call_args[0][0] == "meta_store.db.rt"

    async def test_sandbox_id_in_attributes_positional(self):
        """sandbox_id is included when the method declares the parameter (positional)."""
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        result = await store.get("sbx-123")

        assert result == "sbx-123"
        attrs = {"operation": "get", "method": "get", "sandbox_id": "sbx-123"}
        monitor.record_counter_by_name.assert_any_call("meta_store.success", 1, attrs)

    async def test_sandbox_id_unknown_when_keyword_only(self):
        """sandbox_id passed as keyword is not extracted (consistent with monitor_sandbox_operation)."""
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        await store.get(sandbox_id="sbx-kw")

        attrs = {"operation": "get", "method": "get", "sandbox_id": "unknown"}
        monitor.record_counter_by_name.assert_any_call("meta_store.success", 1, attrs)

    async def test_sandbox_id_fallback_from_first_arg(self):
        """Without sandbox_id param, _extract_sandbox_id falls back to args[0]."""
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        await store.list_by("state", "running")

        attrs = {"operation": "list_by", "method": "list_by", "sandbox_id": "state"}
        monitor.record_counter_by_name.assert_any_call("meta_store.success", 1, attrs)

    async def test_sandbox_id_unknown_when_no_args(self):
        """No args at all → sandbox_id defaults to 'unknown'."""
        monitor = _make_monitor()
        store = FakeStore(metrics_monitor=monitor)

        await store.do_something()

        attrs = {"operation": "do_something", "method": "do_something", "sandbox_id": "unknown"}
        monitor.record_counter_by_name.assert_any_call("meta_store.success", 1, attrs)
