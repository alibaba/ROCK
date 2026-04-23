"""Tests for SandboxTable DB-layer metrics."""

from unittest.mock import Mock, patch

import pytest

from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.metrics.monitor import MetricsMonitor

SANDBOX_ID = "sbx-db-metrics-001"
SANDBOX_INFO = {
    "user_id": "user-1",
    "image": "python:3.11",
    "experiment_id": "exp-1",
    "namespace": "default",
    "cluster_name": "cluster-1",
    "state": "running",
    "host_ip": "10.0.0.1",
    "create_time": "2025-01-01T00:00:00Z",
}


PREFIX = "meta_store.db"


@pytest.fixture
def mock_monitor():
    monitor = Mock(spec=MetricsMonitor)
    monitor._should_skip.return_value = False
    monitor.metric_prefix = PREFIX
    return monitor


@pytest.fixture
def table(db_provider, mock_monitor):
    with patch("rock.admin.core.sandbox_table.MetricsMonitor.create", return_value=mock_monitor):
        return SandboxTable(db_provider)


class TestSandboxTableMetrics:
    async def test_create_records_db_metrics(self, table, mock_monitor):
        await table.create(SANDBOX_ID, SANDBOX_INFO)

        attrs = {"operation": "create", "method": "create", "sandbox_id": SANDBOX_ID}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.success", 1, attrs)
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.total", 1, attrs)
        rt_call = mock_monitor.record_gauge_by_name.call_args
        assert rt_call[0][0] == f"{PREFIX}.rt"
        assert rt_call[0][1] > 0

    async def test_get_records_db_metrics(self, table, mock_monitor):
        await table.create(SANDBOX_ID, SANDBOX_INFO)
        mock_monitor.reset_mock()

        await table.get(SANDBOX_ID)

        attrs = {"operation": "get", "method": "get", "sandbox_id": SANDBOX_ID}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.success", 1, attrs)
        mock_monitor.record_gauge_by_name.assert_called_once()

    async def test_failure_records_error_type(self, table, mock_monitor):
        table._db = Mock()
        table._db.engine = property(lambda self: (_ for _ in ()).throw(RuntimeError("db down")))

        with pytest.raises(Exception):
            await table.get("nonexistent-will-fail")

    async def test_list_by_records_db_metrics(self, table, mock_monitor):
        await table.create(SANDBOX_ID, SANDBOX_INFO)
        mock_monitor.reset_mock()

        results = await table.list_by("user_id", "user-1")

        assert len(results) == 1
        attrs = {"operation": "list_by", "method": "list_by", "sandbox_id": "user_id"}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.success", 1, attrs)
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.total", 1, attrs)
