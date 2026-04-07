# rock/rocklet/monitor.py
"""Per-worker resource monitor that runs as a background asyncio task inside rocklet."""
import asyncio
import socket

import psutil
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from rock import env_vars
from rock.admin.metrics.constants import MetricsConstants
from rock.logger import init_logger
from rock.utils import get_uniagent_endpoint

logger = init_logger(__name__)


def _xrl_gateway_metric_name(name: str) -> str:
    return f"xrl_gateway.{name}"


def _get_worker_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except socket.gaierror:
        return "unknown"


class WorkerMonitorService:
    """Background worker-node resource monitor.

    Collects psutil metrics every 10 s and pushes them to an OTLP endpoint.
    Designed to run as an asyncio background task inside the rocklet process.

    Configuration (environment variables):
        ROCK_DOCKER_ROOT       — docker data-root dir to monitor (optional)
        ROCK_LOGGING_PATH      — log directory to monitor (optional)
    """

    _report_interval: int = 10
    _export_interval_millis: int = 10_000

    def __init__(self) -> None:
        self._node_id: str = socket.gethostname()
        self._worker_ip: str = _get_worker_ip()
        self._docker_root: str | None = env_vars.ROCK_DOCKER_ROOT
        self._log_dir: str | None = env_vars.ROCK_LOGGING_PATH
        self._running: bool = False
        self._gauges: dict = {}
        self._init_metrics()

    def _init_metrics(self) -> None:
        host, port = get_uniagent_endpoint()
        endpoint = f"http://{host}:{port}/v1/metrics"
        logger.info(f"WorkerMonitorService OTLP endpoint: {endpoint}")
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=self._export_interval_millis)
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter(MetricsConstants.METRICS_METER_NAME)

        self._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT] = meter.create_gauge(
            name=_xrl_gateway_metric_name(MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT),
            description="Docker root dir disk usage percent on worker node",
            unit="1",
        )
        self._gauges[MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT] = meter.create_gauge(
            name=_xrl_gateway_metric_name(MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT),
            description="Log dir disk usage percent on worker node",
            unit="1",
        )

    async def start(self) -> None:
        """Start background monitoring loop (idempotent)."""
        if self._running:
            return
        self._running = True
        logger.info(
            f"WorkerMonitorService starting: node_id={self._node_id} "
            f"worker_ip={self._worker_ip} docker_root={self._docker_root!r} "
            f"log_dir={self._log_dir!r}"
        )
        asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                self._collect_and_report()
            except Exception as e:
                logger.warning(f"WorkerMonitorService collect failed on {self._worker_ip}: {e}")
            await asyncio.sleep(self._report_interval)

    def _collect_and_report(self) -> None:
        attrs = {"node_id": self._node_id, "worker_ip": self._worker_ip}

        if self._docker_root:
            try:
                self._gauges[MetricsConstants.WORKER_DISK_DOCKER_DIR_PERCENT].set(
                    psutil.disk_usage(self._docker_root).percent, attributes=attrs
                )
            except Exception as e:
                logger.warning(f"disk_usage({self._docker_root!r}) failed: {e}")

        if self._log_dir:
            try:
                self._gauges[MetricsConstants.WORKER_DISK_LOG_DIR_PERCENT].set(
                    psutil.disk_usage(self._log_dir).percent, attributes=attrs
                )
            except Exception as e:
                logger.warning(f"disk_usage({self._log_dir!r}) failed: {e}")
