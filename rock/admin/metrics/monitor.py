from collections import Counter as CollectionsCounter

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Counter, _Gauge
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader

from rock import env_vars
from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.gc_view_instrument_match import patch_view_instrument_match
from rock.logger import init_logger
from rock.utils import get_instance_id, get_uniagent_endpoint

logger = init_logger(__name__)


class MetricsMonitor:
    def __init__(
        self,
        host: str,
        port: str,
        pod: str,
        env: str = "daily",
        role: str = "test",
        export_interval_millis: int = 10000,
        endpoint: str = "",
    ):
        patch_view_instrument_match()
        self._init_basic_attributes(host, port, pod, env, role)
        self.endpoint = endpoint or f"http://{self.host}:{self.port}/v1/metrics"
        self._init_telemetry(export_interval_millis)
        self.counters: dict[str, Counter] = {}
        self.gauges: dict[str, _Gauge] = {}
        self._register_metrics()
        logger.info(
            f"Initializing MetricsCollector with host={host}, port={port}, pod={pod}, "
            f"env={env}, role={role}, endpoint={self.endpoint}"
        )

    @classmethod
    def create(cls, export_interval_millis: int = 20000, metrics_endpoint: str = "") -> "MetricsMonitor":
        host, port = get_uniagent_endpoint()
        pod = get_instance_id()
        env = env_vars.ROCK_ADMIN_ENV
        role = env_vars.ROCK_ADMIN_ROLE
        logger.info(f"Initializing MetricsCollector with host={host}, port={port}, " f"env={env}, role={role}")
        return cls(
            host=host,
            port=port,
            pod=pod,
            env=env,
            role=role,
            export_interval_millis=export_interval_millis,
            endpoint=metrics_endpoint,
        )

    def _register_metrics(self):
        """Register all monitoring metrics"""
        # Request count metrics
        self._register_counter(MetricsConstants.SANDBOX_REQUEST_SUCCESS, "Number of successful requests")
        self._register_counter(MetricsConstants.SANDBOX_REQUEST_FAILURE, "Number of failed requests")
        self._register_counter(MetricsConstants.SANDBOX_REQUEST_TOTAL, "Number of total requests")
        # Sandbox metrics
        self._register_gauge(MetricsConstants.SANDBOX_TOTAL_COUNT, "Number of sandbox")
        self._register_gauge(MetricsConstants.SANDBOX_COUNT_IMAGE, "Number of sandbox aggregated by image")
        # RT metrics
        self._register_gauge(
            MetricsConstants.SANDBOX_REQUEST_RT,
            "total execution time for request",
            "ms",
        )
        # Single sandbox resource utilization metrics (percentage of allocated resources)
        self._register_gauge(MetricsConstants.SANDBOX_CPU, "Single sandbox CPU usage percentage of allocated resources")
        self._register_gauge(
            MetricsConstants.SANDBOX_MEM, "Single sandbox memory usage percentage of allocated resources"
        )
        self._register_gauge(
            MetricsConstants.SANDBOX_DISK, "Single sandbox disk usage percentage of allocated resources"
        )
        self._register_gauge(
            MetricsConstants.SANDBOX_NET, "Single sandbox network usage percentage of allocated resources"
        )

        # Ray cluster resource metrics (total and available resources)
        self._register_gauge(MetricsConstants.TOTAL_CPU_RESOURCE, "Total CPU resource in Ray cluster")
        self._register_gauge(MetricsConstants.TOTAL_MEM_RESOURCE, "Total memory resource in Ray cluster")
        self._register_gauge(MetricsConstants.AVAILABLE_CPU_RESOURCE, "Available CPU resource in Ray cluster")
        self._register_gauge(MetricsConstants.AVAILABLE_MEM_RESOURCE, "Available memory resource in Ray cluster")

    def _register_counter(self, name: str, description: str, unit: str = "1"):
        self.counters[name] = self.create_counter(name, description, unit)

    def _register_gauge(self, name: str, description: str, unit: str = "1"):
        self.gauges[name] = self.create_gauge(name, description, unit)

    def _init_basic_attributes(self, host: str, port: str, pod: str, env: str, role: str):
        self.host = host
        self.port = port
        self.env = env
        self.role = role
        self.base_attributes = {
            "host": self.host,
            "pod": pod,
            "env": self.env,
            "role": self.role,
        }

    def _should_skip(self):
        if self.env in {"daily", "aliyun", "local"}:
            return True
        return False

    def _init_telemetry(self, export_interval_millis: int):
        if self._should_skip():
            return
        if self.env == "dev":
            self.metric_reader = InMemoryMetricReader()
        else:
            self.otlp_exporter = OTLPMetricExporter(endpoint=self.endpoint)
            self.metric_reader = PeriodicExportingMetricReader(
                self.otlp_exporter,
                export_interval_millis=export_interval_millis,
            )
        self.meter_provider = MeterProvider(metric_readers=[self.metric_reader])
        metrics.set_meter_provider(self.meter_provider)
        self.meter = metrics.get_meter(MetricsConstants.METRICS_METER_NAME)
        logger.info("init telemetry success")

    def create_counter(self, name: str, description: str, unit: str = "1") -> Counter:
        """Create counter"""
        if self._should_skip():
            return
        logger.info(f"Creating counter {name}")
        return self.meter.create_counter(name=f"xrl_gateway.{name}", description=description, unit=unit)

    def create_gauge(self, name: str, description: str, unit: str = "1") -> _Gauge:
        """Create gauge"""
        if self._should_skip():
            return
        logger.info(f"Creating gauge {name}")
        return self.meter.create_gauge(name=f"xrl_gateway.{name}", description=description, unit=unit)

    def record_counter(self, counter: Counter, value: int = 1, attributes: dict[str, str] | None = None):
        if self._should_skip():
            return
        if attributes:
            merged_attributes = {**self.attributes, **attributes}
        else:
            merged_attributes = self.attributes
        counter.add(value, merged_attributes)

    def record_counter_by_name(self, counter: str, value: int = 1, attributes: dict[str, str] | None = None):
        if self._should_skip():
            return
        c: Counter = self.counters[counter]
        if c is None:
            raise ValueError(f"Counter {counter} not found")
        self.record_counter(c, value, attributes)

    def record_gauge(self, gauge: _Gauge, value: float, attributes: dict[str, str] | None = None):
        if self._should_skip():
            return
        if attributes:
            merged_attributes = {**self.attributes, **attributes}
        else:
            merged_attributes = self.attributes
        gauge.set(value, merged_attributes)

    def record_gauge_by_name(self, gauge: str, value: float, attributes: dict[str, str] | None = None):
        if self._should_skip():
            return
        g: _Gauge = self.gauges[gauge]
        if g is None:
            raise ValueError(f"Gauge {gauge} not found")
        self.record_gauge(g, value, attributes)

    @property
    def attributes(self) -> dict[str, str]:
        """Get basic attributes"""
        return self.base_attributes


def aggregate_metrics(metrics: dict[str, dict[str, str]], key: str) -> dict[str, int]:
    return dict(CollectionsCounter(metric_value[key] for metric_value in metrics.values()))
