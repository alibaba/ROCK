"""Unit tests for disk resource metrics collection and reporting."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_collect_system_resource_metrics_includes_disk(sandbox_manager):
    """_collect_system_resource_metrics should return disk total/available from Ray."""
    disk_total_bytes = 100 * 1024**3
    disk_available_bytes = 60 * 1024**3

    with (
        patch("rock.sandbox.base_manager.ray.cluster_resources") as mock_cluster,
        patch("rock.sandbox.base_manager.ray.available_resources") as mock_available,
    ):
        mock_cluster.return_value = {"CPU": 8, "memory": 32 * 1024**3, "disk": disk_total_bytes}
        mock_available.return_value = {"CPU": 4, "memory": 16 * 1024**3, "disk": disk_available_bytes}

        result = await sandbox_manager._collect_system_resource_metrics()

    total_cpu, total_mem, ava_cpu, ava_mem, total_disk, ava_disk = result
    assert total_cpu == 8
    assert total_mem == 32.0
    assert ava_cpu == 4
    assert ava_mem == 16.0
    assert total_disk == 100.0
    assert ava_disk == 60.0


@pytest.mark.asyncio
async def test_collect_system_resource_metrics_no_disk_resource(sandbox_manager):
    """When no worker declares disk, totals should be 0."""
    with (
        patch("rock.sandbox.base_manager.ray.cluster_resources") as mock_cluster,
        patch("rock.sandbox.base_manager.ray.available_resources") as mock_available,
    ):
        mock_cluster.return_value = {"CPU": 8, "memory": 32 * 1024**3}
        mock_available.return_value = {"CPU": 4, "memory": 16 * 1024**3}

        result = await sandbox_manager._collect_system_resource_metrics()

    _, _, _, _, total_disk, ava_disk = result
    assert total_disk == 0.0
    assert ava_disk == 0.0


@pytest.mark.asyncio
async def test_report_system_resource_metrics_records_disk(sandbox_manager):
    """_report_system_resource_metrics should call record_gauge_by_name for disk."""
    from rock.admin.metrics.constants import MetricsConstants

    with (
        patch("rock.sandbox.base_manager.ray.cluster_resources") as mock_cluster,
        patch("rock.sandbox.base_manager.ray.available_resources") as mock_available,
    ):
        mock_cluster.return_value = {"CPU": 8, "memory": 32 * 1024**3, "disk": 100 * 1024**3}
        mock_available.return_value = {"CPU": 4, "memory": 16 * 1024**3, "disk": 60 * 1024**3}

        sandbox_manager.metrics_monitor = MagicMock()

        await sandbox_manager._report_system_resource_metrics()

    calls = {call.args[0]: call.args[1] for call in sandbox_manager.metrics_monitor.record_gauge_by_name.call_args_list}
    assert calls[MetricsConstants.TOTAL_DISK_RESOURCE] == 100.0
    assert calls[MetricsConstants.AVAILABLE_DISK_RESOURCE] == 60.0
    assert calls[MetricsConstants.TOTAL_CPU_RESOURCE] == 8
    assert calls[MetricsConstants.AVAILABLE_CPU_RESOURCE] == 4


@pytest.mark.asyncio
async def test_opensandbox_metrics_skip_ray_system_resources(sandbox_manager, monkeypatch):
    monkeypatch.setattr(sandbox_manager.rock_config.runtime, "operator_type", "opensandbox")
    sandbox_manager._report_system_resource_metrics = AsyncMock()
    sandbox_manager._collect_sandbox_meta = AsyncMock(return_value=(0, {}))

    await sandbox_manager._collect_and_report_metrics_internal()

    sandbox_manager._report_system_resource_metrics.assert_not_awaited()
    sandbox_manager._collect_sandbox_meta.assert_awaited_once()
