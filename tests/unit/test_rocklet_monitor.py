import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.rocklet.monitor import (
    RockletMetricsMonitor,
    start_monitor_process,
    stop_monitor_process,
)


class TestRockletMetricsMonitor:
    """Tests for the RockletMetricsMonitor class."""

    def _make_monitor(self, **kwargs):
        defaults = {
            "sandbox_id": "test-sandbox-001",
            "rocklet_port": 18000,
            "report_interval": 5,
            "export_interval_millis": 5000,
            "env": "test",
            "role": "write",
            "user_id": "user-123",
            "experiment_id": "exp-456",
            "namespace": "test-ns",
            "metrics_endpoint": "http://localhost:4318/v1/metrics",
            "user_defined_tags": {"service": "rock-test"},
        }
        defaults.update(kwargs)
        return RockletMetricsMonitor(**defaults)

    def test_init_default_values(self):
        """Test monitor initializes with correct default values."""
        monitor = RockletMetricsMonitor(sandbox_id="sandbox-1")
        assert monitor._sandbox_id == "sandbox-1"
        assert monitor._rocklet_port == 8000
        assert monitor._report_interval == 10
        assert monitor._export_interval_millis == 10000
        assert monitor._env == "dev"
        assert monitor._role == "test"
        assert monitor._user_id == "default"
        assert monitor._experiment_id == "default"
        assert monitor._namespace == "default"
        assert monitor._metrics_endpoint == ""
        assert monitor._user_defined_tags == {}
        assert monitor._scheduler is None
        assert monitor._http_client is None

    def test_init_custom_values(self):
        """Test monitor initializes with custom values."""
        monitor = self._make_monitor()
        assert monitor._sandbox_id == "test-sandbox-001"
        assert monitor._rocklet_port == 18000
        assert monitor._report_interval == 5
        assert monitor._export_interval_millis == 5000
        assert monitor._env == "test"
        assert monitor._role == "write"
        assert monitor._user_id == "user-123"
        assert monitor._experiment_id == "exp-456"
        assert monitor._namespace == "test-ns"
        assert monitor._metrics_endpoint == "http://localhost:4318/v1/metrics"
        assert monitor._user_defined_tags == {"service": "rock-test"}

    @patch("rock.rocklet.monitor.get_uniagent_endpoint", return_value=("10.0.0.1", "4318"))
    @patch("rock.rocklet.monitor.OTLPMetricExporter")
    @patch("rock.rocklet.monitor.PeriodicExportingMetricReader")
    @patch("rock.rocklet.monitor.MeterProvider")
    @patch("rock.rocklet.monitor.metrics")
    def test_init_otel_with_custom_endpoint(
        self, mock_metrics, mock_meter_provider, mock_reader, mock_exporter, mock_get_endpoint
    ):
        """Test OTEL initialization uses custom metrics_endpoint when provided."""
        monitor = self._make_monitor(metrics_endpoint="http://custom:9090/v1/metrics")
        monitor._init_otel()

        mock_exporter.assert_called_once_with(endpoint="http://custom:9090/v1/metrics")
        assert monitor._host == "10.0.0.1"

    @patch("rock.rocklet.monitor.get_uniagent_endpoint", return_value=("10.0.0.1", "4318"))
    @patch("rock.rocklet.monitor.OTLPMetricExporter")
    @patch("rock.rocklet.monitor.PeriodicExportingMetricReader")
    @patch("rock.rocklet.monitor.MeterProvider")
    @patch("rock.rocklet.monitor.metrics")
    def test_init_otel_with_default_endpoint(
        self, mock_metrics, mock_meter_provider, mock_reader, mock_exporter, mock_get_endpoint
    ):
        """Test OTEL initialization falls back to uniagent endpoint when no custom endpoint."""
        monitor = self._make_monitor(metrics_endpoint="")
        monitor._init_otel()

        mock_exporter.assert_called_once_with(endpoint="http://10.0.0.1:4318/v1/metrics")

    @patch("rock.rocklet.monitor.get_uniagent_endpoint", return_value=("10.0.0.1", "4318"))
    @patch("rock.rocklet.monitor.OTLPMetricExporter")
    @patch("rock.rocklet.monitor.PeriodicExportingMetricReader")
    @patch("rock.rocklet.monitor.MeterProvider")
    @patch("rock.rocklet.monitor.metrics")
    def test_init_otel_registers_gauges(
        self, mock_metrics, mock_meter_provider, mock_reader, mock_exporter, mock_get_endpoint
    ):
        """Test OTEL initialization registers all five gauge metrics."""
        mock_meter = MagicMock()
        mock_metrics.get_meter.return_value = mock_meter

        monitor = self._make_monitor()
        monitor._init_otel()

        assert mock_meter.create_gauge.call_count == 5
        gauge_names = [call.kwargs["name"] for call in mock_meter.create_gauge.call_args_list]
        assert "xrl_gateway.system.cpu" in gauge_names
        assert "xrl_gateway.system.memory" in gauge_names
        assert "xrl_gateway.system.disk" in gauge_names
        assert "xrl_gateway.system.network" in gauge_names
        assert "xrl_gateway.system.lifespan_rt" in gauge_names


class TestRockletMetricsMonitorAsync:
    """Async tests for the RockletMetricsMonitor class."""

    def _make_monitor(self, **kwargs):
        defaults = {
            "sandbox_id": "test-sandbox-001",
            "rocklet_port": 18000,
            "report_interval": 5,
            "export_interval_millis": 5000,
            "env": "test",
            "role": "write",
            "user_id": "user-123",
            "experiment_id": "exp-456",
            "namespace": "test-ns",
            "metrics_endpoint": "http://localhost:4318/v1/metrics",
            "user_defined_tags": {"service": "rock-test"},
        }
        defaults.update(kwargs)
        return RockletMetricsMonitor(**defaults)

    @pytest.mark.asyncio
    async def test_fetch_statistics_success(self):
        """Test successful statistics fetch from rocklet endpoint."""
        monitor = self._make_monitor()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"cpu": 25.0, "mem": 60.0, "disk": 40.0, "net": 1024}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        monitor._http_client = mock_client

        result = await monitor._fetch_statistics()
        assert result == {"cpu": 25.0, "mem": 60.0, "disk": 40.0, "net": 1024}
        mock_client.get.assert_called_once_with("http://localhost:18000/get_statistics", timeout=3)

    @pytest.mark.asyncio
    async def test_fetch_statistics_http_error(self):
        """Test statistics fetch handles HTTP errors gracefully."""
        monitor = self._make_monitor()
        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        monitor._http_client = mock_client

        result = await monitor._fetch_statistics()
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_statistics_connection_error(self):
        """Test statistics fetch handles connection errors gracefully."""
        monitor = self._make_monitor()
        mock_client = AsyncMock()
        mock_client.get.side_effect = ConnectionError("Connection refused")
        monitor._http_client = mock_client

        result = await monitor._fetch_statistics()
        assert result is None

    @pytest.mark.asyncio
    async def test_report_single_sandbox_sets_gauges(self):
        """Test that report_single_sandbox correctly sets all gauge values."""
        monitor = self._make_monitor()
        monitor._host = "10.0.0.1"

        mock_cpu_gauge = MagicMock()
        mock_mem_gauge = MagicMock()
        mock_disk_gauge = MagicMock()
        mock_net_gauge = MagicMock()
        monitor._gauges = {
            "cpu": mock_cpu_gauge,
            "mem": mock_mem_gauge,
            "disk": mock_disk_gauge,
            "net": mock_net_gauge,
        }

        stats = {"cpu": 25.0, "mem": 60.0, "disk": 40.0, "net": 1024}
        with patch.object(monitor, "_fetch_statistics", new_callable=AsyncMock, return_value=stats):
            await monitor._report_single_sandbox()

        expected_attributes = {
            "sandbox_id": "test-sandbox-001",
            "env": "test",
            "role": "write",
            "host": "10.0.0.1",
            "ip": monitor._ip,
            "user_id": "user-123",
            "experiment_id": "exp-456",
            "namespace": "test-ns",
            "service": "rock-test",
        }
        mock_cpu_gauge.set.assert_called_once_with(25.0, attributes=expected_attributes)
        mock_mem_gauge.set.assert_called_once_with(60.0, attributes=expected_attributes)
        mock_disk_gauge.set.assert_called_once_with(40.0, attributes=expected_attributes)
        mock_net_gauge.set.assert_called_once_with(1024, attributes=expected_attributes)

    @pytest.mark.asyncio
    async def test_report_single_sandbox_no_metrics(self):
        """Test that report_single_sandbox handles None metrics gracefully."""
        monitor = self._make_monitor()
        monitor._gauges = {
            "cpu": MagicMock(),
            "mem": MagicMock(),
            "disk": MagicMock(),
            "net": MagicMock(),
        }

        with patch.object(monitor, "_fetch_statistics", new_callable=AsyncMock, return_value=None):
            await monitor._report_single_sandbox()

        for gauge in monitor._gauges.values():
            gauge.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_single_sandbox_cpu_none(self):
        """Test that report_single_sandbox skips when cpu is None."""
        monitor = self._make_monitor()
        monitor._gauges = {
            "cpu": MagicMock(),
            "mem": MagicMock(),
            "disk": MagicMock(),
            "net": MagicMock(),
        }

        stats = {"cpu": None, "mem": 60.0, "disk": 40.0, "net": 1024}
        with patch.object(monitor, "_fetch_statistics", new_callable=AsyncMock, return_value=stats):
            await monitor._report_single_sandbox()

        for gauge in monitor._gauges.values():
            gauge.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_collect_and_report_metrics_timeout(self):
        """Test that _collect_and_report_metrics handles timeout."""
        monitor = self._make_monitor(report_interval=2)

        async def slow_report():
            await asyncio.sleep(10)

        with patch.object(monitor, "_report_single_sandbox", side_effect=slow_report):
            # Should not raise, just log the timeout
            await monitor._collect_and_report_metrics()

    @pytest.mark.asyncio
    @patch("rock.rocklet.monitor.get_uniagent_endpoint", return_value=("localhost", "4318"))
    @patch("rock.rocklet.monitor.OTLPMetricExporter")
    @patch("rock.rocklet.monitor.PeriodicExportingMetricReader")
    @patch("rock.rocklet.monitor.MeterProvider")
    @patch("rock.rocklet.monitor.metrics")
    async def test_start_and_stop(
        self, mock_metrics, mock_meter_provider, mock_reader, mock_exporter, mock_get_endpoint
    ):
        """Test monitor start and stop lifecycle."""
        mock_meter = MagicMock()
        mock_metrics.get_meter.return_value = mock_meter

        monitor = self._make_monitor()
        await monitor.start()

        assert monitor._scheduler is not None
        assert monitor._scheduler.running
        assert monitor._http_client is not None

        await monitor.stop()
        assert monitor._scheduler is None
        assert monitor._http_client is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """Test that stop is safe to call without start."""
        monitor = self._make_monitor()
        # Should not raise
        await monitor.stop()


class TestMonitorProcessManagement:
    """Tests for start_monitor_process and stop_monitor_process functions."""

    @patch("rock.rocklet.monitor.multiprocessing.Process")
    def test_start_monitor_process_with_explicit_sandbox_id(self, mock_process_cls):
        """Test starting monitor process with explicit sandbox_id."""
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        result = start_monitor_process(sandbox_id="my-sandbox", rocklet_port=9000)

        assert result is mock_process
        mock_process.start.assert_called_once()
        call_kwargs = mock_process_cls.call_args
        assert call_kwargs.kwargs["kwargs"]["sandbox_id"] == "my-sandbox"
        assert call_kwargs.kwargs["kwargs"]["rocklet_port"] == 9000
        assert call_kwargs.kwargs["daemon"] is True
        assert call_kwargs.kwargs["name"] == "rocklet-metrics-monitor"

    @patch("rock.rocklet.monitor.multiprocessing.Process")
    def test_start_monitor_process_sandbox_id_from_env(self, mock_process_cls):
        """Test starting monitor process with sandbox_id from environment."""
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        with patch.dict(os.environ, {"SANDBOX_ID": "env-sandbox-123"}):
            start_monitor_process()

        call_kwargs = mock_process_cls.call_args
        assert call_kwargs.kwargs["kwargs"]["sandbox_id"] == "env-sandbox-123"

    @patch("rock.rocklet.monitor.multiprocessing.Process")
    def test_start_monitor_process_sandbox_id_from_hostname(self, mock_process_cls):
        """Test starting monitor process with sandbox_id from HOSTNAME env."""
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        env_without_sandbox_id = {k: v for k, v in os.environ.items() if k != "SANDBOX_ID"}
        env_without_sandbox_id["HOSTNAME"] = "hostname-sandbox"
        with patch.dict(os.environ, env_without_sandbox_id, clear=True):
            start_monitor_process()

        call_kwargs = mock_process_cls.call_args
        assert call_kwargs.kwargs["kwargs"]["sandbox_id"] == "hostname-sandbox"

    @patch("rock.rocklet.monitor.multiprocessing.Process")
    def test_start_monitor_process_default_params(self, mock_process_cls):
        """Test starting monitor process with default parameters."""
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        start_monitor_process(sandbox_id="test")

        call_kwargs = mock_process_cls.call_args.kwargs["kwargs"]
        assert call_kwargs["rocklet_port"] == 8000
        assert call_kwargs["report_interval"] == 10
        assert call_kwargs["export_interval_millis"] == 10000
        assert call_kwargs["env"] == "dev"
        assert call_kwargs["role"] == "test"
        assert call_kwargs["user_id"] == "default"
        assert call_kwargs["experiment_id"] == "default"
        assert call_kwargs["namespace"] == "default"
        assert call_kwargs["metrics_endpoint"] == ""
        assert call_kwargs["user_defined_tags"] == {}

    def test_stop_monitor_process_none(self):
        """Test stop_monitor_process with None process."""
        # Should not raise
        stop_monitor_process(None)

    def test_stop_monitor_process_not_alive(self):
        """Test stop_monitor_process with a process that is not alive."""
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        # Should not raise and should not call terminate
        stop_monitor_process(mock_process)
        mock_process.terminate.assert_not_called()

    def test_stop_monitor_process_alive_graceful(self):
        """Test stop_monitor_process gracefully terminates a running process."""
        mock_process = MagicMock()
        mock_process.is_alive.side_effect = [True, False]
        mock_process.pid = 12345

        stop_monitor_process(mock_process)

        mock_process.terminate.assert_called_once()
        mock_process.join.assert_called_once_with(timeout=5)
        mock_process.kill.assert_not_called()

    def test_stop_monitor_process_alive_force_kill(self):
        """Test stop_monitor_process force-kills a process that won't terminate."""
        mock_process = MagicMock()
        mock_process.is_alive.side_effect = [True, True]
        mock_process.pid = 12345

        stop_monitor_process(mock_process)

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()
        assert mock_process.join.call_count == 2


class TestBaseActorMonitorSwitch:
    """Tests for the ROCK_MONITOR_VIA_ROCKLET switch in BaseActor."""

    @pytest.mark.asyncio
    @patch("rock.sandbox.base_actor.env_vars")
    async def test_setup_monitor_skipped_when_monitor_disabled(self, mock_env_vars):
        """Test _setup_monitor does nothing when ROCK_MONITOR_ENABLE is False."""
        mock_env_vars.ROCK_MONITOR_ENABLE = False
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = False

        from rock.sandbox.base_actor import BaseActor

        mock_config = MagicMock()
        mock_config.auto_clear_time = None
        # mock_deployment = MagicMock()

        with patch.object(BaseActor, "__init__", lambda self, *a, **kw: None):
            actor = BaseActor.__new__(BaseActor)
            actor._metrics_report_scheduler = None

        await actor._setup_monitor()
        # _init_monitor should not have been called — no scheduler created
        assert actor._metrics_report_scheduler is None

    @pytest.mark.asyncio
    @patch("rock.sandbox.base_actor.env_vars")
    async def test_setup_monitor_skipped_when_via_rocklet_enabled(self, mock_env_vars):
        """Test _setup_monitor skips actor monitoring when ROCK_MONITOR_VIA_ROCKLET is True."""
        mock_env_vars.ROCK_MONITOR_ENABLE = True
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = True

        from rock.sandbox.base_actor import BaseActor

        with patch.object(BaseActor, "__init__", lambda self, *a, **kw: None):
            actor = BaseActor.__new__(BaseActor)
            actor._metrics_report_scheduler = None

        await actor._setup_monitor()
        # _init_monitor should not have been called — no scheduler created
        assert actor._metrics_report_scheduler is None

    @patch("rock.sandbox.base_actor.env_vars")
    def test_stop_monitoring_noop_when_via_rocklet(self, mock_env_vars):
        """Test stop_monitoring does nothing when ROCK_MONITOR_VIA_ROCKLET is True."""
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = True

        from rock.sandbox.base_actor import BaseActor

        with patch.object(BaseActor, "__init__", lambda self, *a, **kw: None):
            actor = BaseActor.__new__(BaseActor)
            mock_scheduler = MagicMock()
            mock_scheduler.running = True
            actor._metrics_report_scheduler = mock_scheduler

        actor.stop_monitoring()
        mock_scheduler.shutdown.assert_not_called()

    @patch("rock.sandbox.base_actor.env_vars")
    def test_stop_monitoring_works_when_via_rocklet_disabled(self, mock_env_vars):
        """Test stop_monitoring works normally when ROCK_MONITOR_VIA_ROCKLET is False."""
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = False
        mock_env_vars.ROCK_MONITOR_ENABLE = True

        from rock.sandbox.base_actor import BaseActor

        with patch.object(BaseActor, "__init__", lambda self, *a, **kw: None):
            actor = BaseActor.__new__(BaseActor)
            mock_scheduler = MagicMock()
            mock_scheduler.running = True
            actor._metrics_report_scheduler = mock_scheduler

        actor.stop_monitoring()
        mock_scheduler.shutdown.assert_called_once_with(wait=True)


class TestServerMonitorIntegration:
    """Tests for the rocklet server startup/shutdown monitor integration."""

    @pytest.mark.asyncio
    @patch("rock.rocklet.server.env_vars")
    @patch("rock.rocklet.server.start_monitor_process")
    async def test_startup_starts_monitor_when_both_flags_enabled(self, mock_start, mock_env_vars):
        """Test that startup event starts monitor when both flags are enabled."""
        mock_env_vars.ROCK_MONITOR_ENABLE = True
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = True
        mock_process = MagicMock()
        mock_start.return_value = mock_process

        import rock.rocklet.server as server_module

        server_module._monitor_process = None
        await server_module.startup_event()

        mock_start.assert_called_once()
        assert server_module._monitor_process is mock_process

    @pytest.mark.asyncio
    @patch("rock.rocklet.server.env_vars")
    @patch("rock.rocklet.server.start_monitor_process")
    async def test_startup_skips_monitor_when_monitor_disabled(self, mock_start, mock_env_vars):
        """Test that startup event skips monitor when ROCK_MONITOR_ENABLE is False."""
        mock_env_vars.ROCK_MONITOR_ENABLE = False
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = True

        import rock.rocklet.server as server_module

        server_module._monitor_process = None
        await server_module.startup_event()

        mock_start.assert_not_called()
        assert server_module._monitor_process is None

    @pytest.mark.asyncio
    @patch("rock.rocklet.server.env_vars")
    @patch("rock.rocklet.server.start_monitor_process")
    async def test_startup_skips_monitor_when_via_rocklet_disabled(self, mock_start, mock_env_vars):
        """Test that startup event skips monitor when ROCK_MONITOR_VIA_ROCKLET is False."""
        mock_env_vars.ROCK_MONITOR_ENABLE = True
        mock_env_vars.ROCK_MONITOR_VIA_ROCKLET = False

        import rock.rocklet.server as server_module

        server_module._monitor_process = None
        await server_module.startup_event()

        mock_start.assert_not_called()
        assert server_module._monitor_process is None

    @pytest.mark.asyncio
    @patch("rock.rocklet.server.stop_monitor_process")
    async def test_shutdown_stops_monitor(self, mock_stop):
        """Test that shutdown event stops the monitor process."""
        import rock.rocklet.server as server_module

        mock_process = MagicMock()
        server_module._monitor_process = mock_process

        await server_module.shutdown_event()

        mock_stop.assert_called_once_with(mock_process)
        assert server_module._monitor_process is None

    @pytest.mark.asyncio
    @patch("rock.rocklet.server.stop_monitor_process")
    async def test_shutdown_noop_when_no_monitor(self, mock_stop):
        """Test that shutdown event is safe when no monitor process exists."""
        import rock.rocklet.server as server_module

        server_module._monitor_process = None

        await server_module.shutdown_event()

        mock_stop.assert_not_called()
