import asyncio
import datetime
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import _Gauge
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from rock import env_vars
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.logger import init_logger
from rock.utils import get_uniagent_endpoint

logger = init_logger(__name__)


class BaseActor:
    _config: DeploymentConfig
    _deployment: AbstractDeployment
    _metrics_report_scheduler = None
    _auto_clear_time_in_minutes: int = 30
    _export_interval_millis: int = 10000  # Default value that can be overridden by subclasses
    _stop_time: datetime.datetime = None
    _gauges: dict
    host: str = None  # Will be set by subclasses that need it
    _role: str = "test"
    _env: str = "dev"
    _user_id: str = "default"
    _experiment_id: str = "default"
    _namespace = "default"

    def __init__(
        self,
        config: DeploymentConfig,
        deployment: AbstractDeployment,
    ):
        self._config = config
        self._deployment = deployment
        self._gauges: dict[str, _Gauge] = {}
        if isinstance(config, DockerDeploymentConfig) and config.auto_clear_time:
            self._auto_clear_time_in_minutes = config.auto_clear_time
        self._stop_time = datetime.datetime.now() + datetime.timedelta(minutes=self._auto_clear_time_in_minutes)
        # Initialize the user and environment info - can be overridden by subclasses
        self._role = "test"
        self._env = "dev"
        self._user_id = "default"
        self._experiment_id = "default"

    async def deployment_config(self) -> DeploymentConfig | None:
        return self._config

    def get_mount(self) -> str:
        if isinstance(self._deployment, DockerDeployment):
            v_commands: list[str] = self._deployment._prepare_volume_mounts()
            return " ".join(v_commands)
        return ""

    async def _refresh_stop_time(self):
        if isinstance(self._deployment, DockerDeployment):
            await self._deployment.refresh_stop_time()

        self._stop_time = datetime.datetime.now() + datetime.timedelta(minutes=self._auto_clear_time_in_minutes)
        logger.info(f"refresh stop_time to {self._stop_time}")

    def _init_monitor(self):
        """
        Initialize the metrics monitoring system using OTLP exporter.
        This method sets up the metrics collection infrastructure and registers gauge metrics.
        """
        host, port = get_uniagent_endpoint()
        env = self._env
        role = self._role
        self.host = host
        logger.info(f"Initializing MetricsCollector with host={host}, port={port}, " f"env={env}, role={role}")
        self.otlp_exporter = OTLPMetricExporter(endpoint=f"http://{host}:{port}/v1/metrics")
        self.metric_reader = PeriodicExportingMetricReader(
            self.otlp_exporter,
            export_interval_millis=self._export_interval_millis,
        )
        self.meter_provider = MeterProvider(metric_readers=[self.metric_reader])
        metrics.set_meter_provider(self.meter_provider)
        self.meter = metrics.get_meter("XRL_GATEWAY_CONFIG")

        # register metrics
        self._gauges["cpu"] = self.meter.create_gauge(name="xrl_gateway.system.cpu", description="CPU Usage", unit="1")
        self._gauges["mem"] = self.meter.create_gauge(
            name="xrl_gateway.system.memory", description="Memory Usage", unit="1"
        )
        self._gauges["disk"] = self.meter.create_gauge(
            name="xrl_gateway.system.disk", description="Disk Usage", unit="1"
        )
        self._gauges["net"] = self.meter.create_gauge(
            name="xrl_gateway.system.network", description="Network Usage", unit="1"
        )

    async def _setup_monitor(self):
        if not env_vars.ROCK_MONITOR_ENABLE:
            return
        self._init_monitor()
        self._report_interval = 10
        self._metrics_report_scheduler = AsyncIOScheduler(
            timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
        )
        self._metrics_report_scheduler.add_job(
            func=self._collect_and_report_metrics,
            trigger=IntervalTrigger(seconds=self._report_interval),
            id="metrics_collection",
            name="Sandbox Resource Metrics Collection",
        )
        self._metrics_report_scheduler.start()

    def stop_monitoring(self):
        if env_vars.ROCK_MONITOR_ENABLE and self._metrics_report_scheduler and self._metrics_report_scheduler.running:
            logger.info("Stopping APScheduler...")
            self._metrics_report_scheduler.shutdown(wait=True)
            logger.info("APScheduler stopped")

    async def _collect_and_report_metrics(self):
        start = time.perf_counter()
        total_timeout = self._report_interval - 1

        try:
            # Wrap the entire method with a total timeout
            await asyncio.wait_for(self._collect_sandbox_metrics(self._config.container_name), timeout=total_timeout)
        except asyncio.TimeoutError:
            duration = time.perf_counter() - start
            logger.error(f"Metrics collection timed out after {duration:.2f}s (limit: {total_timeout}s)")

    async def _collect_sandbox_metrics(self, sandbox_id: str):
        """Collect metrics for a single sandbox and report them"""
        start = time.perf_counter()
        single_sandbox_timeout = 3
        try:
            try:
                metrics = await asyncio.wait_for(self.get_sandbox_statistics(), timeout=single_sandbox_timeout)
            except asyncio.TimeoutError:
                duration = time.perf_counter() - start
                logger.error(
                    f"sandbox [{sandbox_id}] Metrics collection timed out after {duration:.4f}s (limit: {single_sandbox_timeout}s)"
                )
                return
            logger.debug(f"sandbox [{sandbox_id}] metrics = {metrics}")

            if metrics.get("cpu") is not None:
                attributes = {"sandbox_id": sandbox_id, "env": self._env, "role": self._role, "host": self.host}
                attributes["user_id"] = self._user_id
                attributes["experiment_id"] = self._experiment_id
                attributes["namespace"] = self._namespace
                self._gauges["cpu"].set(metrics["cpu"], attributes=attributes)
                self._gauges["mem"].set(metrics["mem"], attributes=attributes)
                self._gauges["disk"].set(metrics["disk"], attributes=attributes)
                self._gauges["net"].set(metrics["net"], attributes=attributes)

                logger.debug(f"Successfully reported metrics for sandbox: {sandbox_id}")
            else:
                logger.warning(f"No metrics returned for sandbox: {sandbox_id}")
            single_sandbox_report_rt = time.perf_counter() - start
            logger.debug(f"Single sandbox report rt:{single_sandbox_report_rt:.4f}s")

        except Exception as e:
            logger.error(f"Error collecting metrics for sandbox {sandbox_id}: {e}")

    def __del__(self):
        """Destructor, ensure resource cleanup"""
        try:
            self.stop_monitoring()
        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}")
            pass

    async def get_sandbox_statistics(self):
        """Get sandbox statistics - default implementation returns None"""
        if isinstance(self._deployment, DockerDeployment):
            return await self._deployment.runtime.get_statistics()
        return None
