# tests/unit/rocklet/test_worker_monitor.py
"""Unit tests for WorkerMonitorService in rock/rocklet/monitor.py."""
from unittest.mock import MagicMock

import pytest

from rock.admin.metrics.constants import MetricsConstants
from rock.rocklet.monitor import WorkerMonitorService

_ALL_GAUGE_KEYS = [
    MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT,
    MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT,
]

_DISK_DOCKER = 55.0
_DISK_LOG = 30.0


def _make_service(docker_root="/var/lib/docker", log_dir="/var/log/rock") -> WorkerMonitorService:
    """Create a WorkerMonitorService with mocked gauges (no real OTLP)."""
    svc = WorkerMonitorService.__new__(WorkerMonitorService)
    svc._node_id = "node-abc"
    svc._worker_ip = "10.0.0.5"
    svc._docker_root = docker_root
    svc._log_dir = log_dir
    svc._running = False
    svc._gauges = {key: MagicMock() for key in _ALL_GAUGE_KEYS}
    return svc


def _patch_psutil(monkeypatch):
    disk_values = {"/var/lib/docker": _DISK_DOCKER, "/var/log/rock": _DISK_LOG}

    def fake_disk_usage(path):
        m = MagicMock()
        m.percent = disk_values.get(path, 0.0)
        return m

    monkeypatch.setattr("rock.rocklet.monitor.psutil.disk_usage", fake_disk_usage)


def test_collect_reports_docker_dir_disk_percent(monkeypatch):
    svc = _make_service()
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    svc._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT].set.assert_called_once_with(
        _DISK_DOCKER, attributes={"node_id": "node-abc", "worker_ip": "10.0.0.5"}
    )


def test_collect_reports_log_dir_disk_percent(monkeypatch):
    svc = _make_service()
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    svc._gauges[MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT].set.assert_called_once_with(
        _DISK_LOG, attributes={"node_id": "node-abc", "worker_ip": "10.0.0.5"}
    )


def test_docker_dir_gauge_skipped_when_docker_root_is_none(monkeypatch):
    svc = _make_service(docker_root=None)
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    svc._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT].set.assert_not_called()


def test_log_dir_gauge_skipped_when_log_dir_is_none(monkeypatch):
    svc = _make_service(log_dir=None)
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    svc._gauges[MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT].set.assert_not_called()


def test_both_gauges_skipped_when_both_dirs_absent(monkeypatch):
    svc = _make_service(docker_root=None, log_dir=None)
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    svc._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT].set.assert_not_called()
    svc._gauges[MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT].set.assert_not_called()


def test_attributes_contain_node_id_and_worker_ip(monkeypatch):
    svc = _make_service()
    svc._node_id = "node-xyz"
    svc._worker_ip = "192.168.1.99"
    _patch_psutil(monkeypatch)
    svc._collect_and_report()
    call_kwargs = svc._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT].set.call_args[1]
    assert call_kwargs["attributes"]["node_id"] == "node-xyz"
    assert call_kwargs["attributes"]["worker_ip"] == "192.168.1.99"


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch):
    svc = _make_service()
    tasks_created = []

    def fake_create_task(coro):
        tasks_created.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr("rock.rocklet.monitor.asyncio.create_task", fake_create_task)
    await svc.start()
    await svc.start()
    assert len(tasks_created) == 1
