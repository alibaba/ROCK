"""
Rocklet metrics monitor module.

This module extracts the monitoring logic from BaseActor so that it can run
inside the rocklet process (or as a subprocess spawned by rocklet).  When the
feature switch ROCK_MONITOR_VIA_ROCKLET is enabled, the rocklet is responsible
for collecting sandbox resource metrics and reporting them via OTLP, instead of
the Ray actor.
"""

import asyncio
import multiprocessing
import os
import signal
import time

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from rock import env_vars
from rock.logger import init_logger
from rock.utils.system import get_instance_id, get_uniagent_endpoint

logger = init_logger(__name__)

DEFAULT_REPORT_INTERVAL = 10
DEFAULT_EXPORT_INTERVAL_MILLIS = 10000
DEFAULT_ROCKLET_PORT = 8000


class RockletMetricsMonitor:
    """Metrics monitor that runs inside the rocklet process.

    It periodically fetches sandbox resource statistics from the local rocklet
    HTTP endpoint and reports them via OpenTelemetry OTLP exporter.
    """

    def __init__(
        self,
        sandbox_id: str,
        rocklet_port: int = DEFAULT_ROCKLET_PORT,
        report_interval: int = DEFAULT_REPORT_INTERVAL,
        export_interval_millis: int = DEFAULT_EXPORT_INTERVAL_MILLIS,
        env: str = "dev",
        role: str = "test",
        user_id: str = "default",
        experiment_id: str = "default",
        namespace: str = "default",
        metrics_endpoint: str = "",
        user_defined_tags: dict | None = None,
    ):
        self._sandbox_id = sandbox_id
        self._rocklet_port = rocklet_port
        self._report_interval = report_interval
        self._export_interval_millis = export_interval_millis
        self._env = env
        self._role = role
        self._user_id = user_id
        self._experiment_id = experiment_id
        self._namespace = namespace
        self._metrics_endpoint = metrics_endpoint
        self._user_defined_tags = user_defined_tags or {}
        self._ip = get_instance_id()
        self._host: str | None = None
        self._gauges: dict = {}
        self._scheduler: AsyncIOScheduler | None = None
        self._http_client: httpx.AsyncClient | None = None

    def _init_otel(self):
        """Initialize the OpenTelemetry metrics pipeline."""
        host, port = get_uniagent_endpoint()
        self._host = host
        logger.info(
            f"RockletMetricsMonitor initializing OTLP with host={host}, port={port}, "
            f"env={self._env}, role={self._role}"
        )
        endpoint = self._metrics_endpoint or f"http://{host}:{port}/v1/metrics"
        otlp_exporter = OTLPMetricExporter(endpoint=endpoint)
        metric_reader = PeriodicExportingMetricReader(
            otlp_exporter,
            export_interval_millis=self._export_interval_millis,
        )
        meter_provider = MeterProvider(metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter("XRL_GATEWAY_CONFIG")

        self._gauges["cpu"] = meter.create_gauge(name="xrl_gateway.system.cpu", description="CPU Usage", unit="1")
        self._gauges["mem"] = meter.create_gauge(name="xrl_gateway.system.memory", description="Memory Usage", unit="1")
        self._gauges["disk"] = meter.create_gauge(name="xrl_gateway.system.disk", description="Disk Usage", unit="1")
        self._gauges["net"] = meter.create_gauge(
            name="xrl_gateway.system.network", description="Network Usage", unit="1"
        )
        self._gauges["rt"] = meter.create_gauge(
            name="xrl_gateway.system.lifespan_rt", description="Life Span Rt", unit="1"
        )

    async def _fetch_statistics(self) -> dict | None:
        """Fetch sandbox statistics from the local rocklet HTTP endpoint."""
        url = f"http://localhost:{self._rocklet_port}/get_statistics"
        try:
            response = await self._http_client.get(url, timeout=3)
            if response.status_code == 200:
                return response.json()
            logger.warning(f"Unexpected status {response.status_code} from {url}")
        except Exception as e:
            logger.error(f"Failed to fetch statistics from rocklet: {e}")
        return None

    async def _collect_and_report_metrics(self):
        """Collect metrics from rocklet and report via OTLP gauges."""
        start = time.perf_counter()
        total_timeout = self._report_interval - 1
        try:
            await asyncio.wait_for(self._report_single_sandbox(), timeout=total_timeout)
        except asyncio.TimeoutError:
            duration = time.perf_counter() - start
            logger.error(f"Metrics collection timed out after {duration:.2f}s (limit: {total_timeout}s)")

    async def _report_single_sandbox(self):
        """Fetch and report metrics for the sandbox."""
        start = time.perf_counter()
        try:
            stats = await self._fetch_statistics()
            if stats is None or stats.get("cpu") is None:
                logger.warning(f"No metrics returned for sandbox: {self._sandbox_id}")
                return

            logger.debug(f"sandbox [{self._sandbox_id}] metrics = {stats}")

            attributes = {
                "sandbox_id": self._sandbox_id,
                "env": self._env,
                "role": self._role,
                "host": self._host or "",
                "ip": self._ip,
                "user_id": self._user_id,
                "experiment_id": self._experiment_id,
                "namespace": self._namespace,
            }
            if self._user_defined_tags:
                attributes.update(self._user_defined_tags)

            self._gauges["cpu"].set(stats["cpu"], attributes=attributes)
            self._gauges["mem"].set(stats["mem"], attributes=attributes)
            self._gauges["disk"].set(stats["disk"], attributes=attributes)
            self._gauges["net"].set(stats["net"], attributes=attributes)

            if env_vars.ROCK_SANDBOX_CREATED_TIME is not None:
                lifespan_rt = time.time() - env_vars.ROCK_SANDBOX_CREATED_TIME
                self._gauges["rt"].set(lifespan_rt, attributes=attributes)

            logger.debug(f"Successfully reported metrics for sandbox: {self._sandbox_id}")
            report_rt = time.perf_counter() - start
            logger.debug(f"Single sandbox report rt: {report_rt:.4f}s")
        except Exception as e:
            logger.error(f"Error collecting metrics for sandbox {self._sandbox_id}: {e}")

    async def start(self):
        """Start the metrics collection scheduler."""
        self._init_otel()
        self._http_client = httpx.AsyncClient()
        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
        )
        self._scheduler.add_job(
            func=self._collect_and_report_metrics,
            trigger=IntervalTrigger(seconds=self._report_interval),
            id="rocklet_metrics_collection",
            name="Rocklet Sandbox Resource Metrics Collection",
        )
        self._scheduler.start()
        logger.info(
            f"RockletMetricsMonitor started for sandbox={self._sandbox_id}, " f"interval={self._report_interval}s"
        )

    async def stop(self):
        """Stop the metrics collection scheduler and clean up resources."""
        if self._scheduler and self._scheduler.running:
            logger.info("Stopping RockletMetricsMonitor scheduler...")
            self._scheduler.shutdown(wait=True)
            logger.info("RockletMetricsMonitor scheduler stopped")
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


def _run_monitor_async(
    sandbox_id: str,
    rocklet_port: int,
    report_interval: int,
    export_interval_millis: int,
    env: str,
    role: str,
    user_id: str,
    experiment_id: str,
    namespace: str,
    metrics_endpoint: str,
    user_defined_tags: dict,
):
    """Entry point for the monitor subprocess — runs the async event loop."""
    monitor = RockletMetricsMonitor(
        sandbox_id=sandbox_id,
        rocklet_port=rocklet_port,
        report_interval=report_interval,
        export_interval_millis=export_interval_millis,
        env=env,
        role=role,
        user_id=user_id,
        experiment_id=experiment_id,
        namespace=namespace,
        metrics_endpoint=metrics_endpoint,
        user_defined_tags=user_defined_tags,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(signum, frame):
        logger.info(f"Monitor subprocess received signal {signum}, shutting down...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        loop.run_until_complete(monitor.start())
        loop.run_forever()
    finally:
        loop.run_until_complete(monitor.stop())
        loop.close()
        logger.info("Monitor subprocess exited")


def start_monitor_process(
    sandbox_id: str = "",
    rocklet_port: int = DEFAULT_ROCKLET_PORT,
    report_interval: int = DEFAULT_REPORT_INTERVAL,
    export_interval_millis: int = DEFAULT_EXPORT_INTERVAL_MILLIS,
    env: str = "dev",
    role: str = "test",
    user_id: str = "default",
    experiment_id: str = "default",
    namespace: str = "default",
    metrics_endpoint: str = "",
    user_defined_tags: dict | None = None,
) -> multiprocessing.Process:
    """Spawn a child process that runs the metrics monitor.

    Returns the ``multiprocessing.Process`` handle so the caller can manage its
    lifecycle (e.g. terminate on shutdown).
    """
    if not sandbox_id:
        sandbox_id = os.getenv("SANDBOX_ID", os.getenv("HOSTNAME", "unknown"))

    process = multiprocessing.Process(
        target=_run_monitor_async,
        kwargs={
            "sandbox_id": sandbox_id,
            "rocklet_port": rocklet_port,
            "report_interval": report_interval,
            "export_interval_millis": export_interval_millis,
            "env": env,
            "role": role,
            "user_id": user_id,
            "experiment_id": experiment_id,
            "namespace": namespace,
            "metrics_endpoint": metrics_endpoint,
            "user_defined_tags": user_defined_tags or {},
        },
        daemon=True,
        name="rocklet-metrics-monitor",
    )
    process.start()
    logger.info(f"Monitor subprocess started with pid={process.pid}")
    return process


def stop_monitor_process(process: multiprocessing.Process | None):
    """Gracefully stop the monitor subprocess."""
    if process is None or not process.is_alive():
        return
    logger.info(f"Terminating monitor subprocess pid={process.pid}")
    process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        logger.warning(f"Monitor subprocess pid={process.pid} did not exit, killing")
        process.kill()
        process.join(timeout=2)
    logger.info("Monitor subprocess stopped")
