"""Tests for SandboxMetaStore metrics integration."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fakeredis import aioredis

from rock.actions.sandbox.response import State
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.metrics.monitor import MetricsMonitor
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.utils.providers.redis_provider import RedisProvider

SANDBOX_ID = "sbx-metrics-001"
SANDBOX_INFO = {
    "sandbox_id": SANDBOX_ID,
    "user_id": "user-1",
    "image": "python:3.11",
    "experiment_id": "exp-1",
    "namespace": "default",
    "cluster_name": "cluster-1",
    "state": State.RUNNING,
    "host_ip": "10.0.0.1",
    "create_time": "2025-01-01T00:00:00Z",
}


PREFIX = "meta_store"
DB_PREFIX = "meta_store.db"


def _make_monitor(prefix=PREFIX):
    monitor = Mock(spec=MetricsMonitor)
    monitor._should_skip.return_value = False
    monitor.metric_prefix = prefix
    return monitor


@pytest.fixture
async def redis():
    provider = RedisProvider(host=None, port=None, password="")
    provider.client = aioredis.FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


@pytest.fixture
def mock_monitor():
    return _make_monitor(PREFIX)


@pytest.fixture
def store(redis, db_provider, mock_monitor):
    db_monitor = _make_monitor(DB_PREFIX)
    with patch("rock.admin.core.sandbox_table.MetricsMonitor.create", return_value=db_monitor):
        table = SandboxTable(db_provider)
    with patch("rock.sandbox.sandbox_meta_store.MetricsMonitor.create", return_value=mock_monitor):
        return SandboxMetaStore(redis_provider=redis, sandbox_table=table)


class TestMetaStoreMetrics:
    async def test_create_records_store_metrics(self, store, mock_monitor):
        await store.create(SANDBOX_ID, SANDBOX_INFO)

        attrs = {"operation": "create", "method": "create", "sandbox_id": SANDBOX_ID}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.success", 1, attrs)
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.total", 1, attrs)
        assert mock_monitor.record_gauge_by_name.called
        rt_call = mock_monitor.record_gauge_by_name.call_args
        assert rt_call[0][0] == f"{PREFIX}.rt"
        assert rt_call[0][1] > 0

    async def test_get_records_store_metrics(self, store, redis, mock_monitor):
        await store.create(SANDBOX_ID, SANDBOX_INFO)
        mock_monitor.reset_mock()

        await store.get(SANDBOX_ID)

        attrs = {"operation": "get", "method": "get", "sandbox_id": SANDBOX_ID}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.success", 1, attrs)
        mock_monitor.record_gauge_by_name.assert_called_once()

    async def test_failure_records_error_type(self, store, redis, mock_monitor):
        redis.json_get = AsyncMock(side_effect=ConnectionError("redis down"))

        with pytest.raises(ConnectionError):
            await store.get(SANDBOX_ID)

        error_attrs = {"operation": "get", "method": "get", "sandbox_id": SANDBOX_ID, "error_type": "ConnectionError"}
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.failure", 1, error_attrs)
        mock_monitor.record_counter_by_name.assert_any_call(f"{PREFIX}.total", 1, error_attrs)
