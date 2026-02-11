import asyncio
import time

import ray
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from rock.admin.core.redis_key import ALIVE_PREFIX
from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.monitor import MetricsMonitor, aggregate_metrics
from rock.config import RockConfig
from rock.deployments.manager import DeploymentManager
from rock.logger import init_logger
from rock.utils import get_executor
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)


class BaseManager:
    _check_job_bg_task: object = None
    _redis_provider: RedisProvider = None
    rock_config: RockConfig = None

    def __init__(
        self,
        rock_config: RockConfig,
        redis_provider: RedisProvider | None = None,
        enable_runtime_auto_clear: bool = False,
    ):
        self.rock_config = rock_config
        self._executor = get_executor()
        self._redis_provider = redis_provider
        self.metrics_monitor = MetricsMonitor.create(
            export_interval_millis=20_000,
            metrics_endpoint=rock_config.runtime.metrics_endpoint,
        )
        self._report_interval = 10
        self._check_job_interval = 180
        self._sandbox_meta = {}
        self._setup_scheduler()
        self.deployment_manager = DeploymentManager(rock_config, enable_runtime_auto_clear)

        logger.info(f"SandboxService initialized with monitoring interval: {self._report_interval}s")

    def _setup_scheduler(self):
        self._setup_metrics_scheduler()
        self._setup_job_check_scheduler()

    def _setup_metrics_scheduler(self):
        """Set up scheduler"""
        self._metrics_scheduler = AsyncIOScheduler(
            timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
        )

        self._metrics_scheduler.add_job(
            func=self._collect_and_report_metrics,
            trigger=IntervalTrigger(seconds=self._report_interval),
            id="metrics_collection",
            name="Sandbox Metrics Collection",
        )
        self._metrics_scheduler.start()
        logger.info("APScheduler started for metrics collection")

    def _setup_job_check_scheduler(self):
        """Set up scheduler"""
        self.scheduler = AsyncIOScheduler(
            timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
        )
        self.scheduler.add_job(
            func=self._check_job_background,
            trigger=IntervalTrigger(seconds=self._check_job_interval),
            id="job_check",
            name="Sandbox Job Check",
        )
        self.scheduler.start()
        logger.info("APScheduler started for job check")

    async def _collect_and_report_metrics(self):
        start_time = time.time()
        total_timeout = self._report_interval - 1

        try:
            await asyncio.wait_for(self._collect_and_report_metrics_internal(), timeout=total_timeout)

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            logger.error(f"Metrics collection timed out after {duration:.2f}s (limit: {total_timeout}s)")

    async def _collect_and_report_metrics_internal(self):
        """Collect and report metrics for all sandboxes"""
        overall_start = time.perf_counter()
        if not self._redis_provider:
            return
        await self._report_system_resource_metrics()

        if not await self._redis_provider.pattern_exists(f"{ALIVE_PREFIX}*"):
            logger.debug("No sandboxes to monitor")
            self.metrics_monitor.record_gauge_by_name(MetricsConstants.SANDBOX_TOTAL_COUNT, 0)
            return

        sandbox_cnt, sandbox_meta = await self._collect_sandbox_meta()
        aggregated_metrics = aggregate_metrics(sandbox_meta, "image")
        for image, count in aggregated_metrics.items():
            self.metrics_monitor.record_gauge_by_name(MetricsConstants.SANDBOX_COUNT_IMAGE, count, {"image": image})

        logger.debug(f"Collecting metrics for {sandbox_cnt} sandboxes")

        self.metrics_monitor.record_gauge_by_name(MetricsConstants.SANDBOX_TOTAL_COUNT, sandbox_cnt)

        overall_duration = time.perf_counter() - overall_start
        logger.debug(f"Metrics overall report rt:{overall_duration:.4f}s")

    async def _report_system_resource_metrics(self):
        """汇报系统资源指标"""
        total_cpu, total_mem, available_cpu, available_mem = await self._collect_system_resource_metrics()
        self.metrics_monitor.record_gauge_by_name(MetricsConstants.TOTAL_CPU_RESOURCE, total_cpu)
        self.metrics_monitor.record_gauge_by_name(MetricsConstants.TOTAL_MEM_RESOURCE, total_mem)
        self.metrics_monitor.record_gauge_by_name(MetricsConstants.AVAILABLE_CPU_RESOURCE, available_cpu)
        self.metrics_monitor.record_gauge_by_name(MetricsConstants.AVAILABLE_MEM_RESOURCE, available_mem)

    async def _collect_system_resource_metrics(self):
        """收集系统资源指标"""
        cluster_resources = ray.cluster_resources()
        available_resources = ray.available_resources()
        total_cpu = cluster_resources.get("CPU", 0)
        total_mem = cluster_resources.get("memory", 0) / 1024**3
        available_cpu = available_resources.get("CPU", 0)
        available_mem = available_resources.get("memory", 0) / 1024**3
        return total_cpu, total_mem, available_cpu, available_mem

    async def _collect_sandbox_meta(self) -> tuple[int, dict[str, dict[str, str]]]:
        meta: dict = {}
        cnt = 0
        # type: ignore
        async for key in self._redis_provider.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=100):
            sandbox_id = key.removeprefix(ALIVE_PREFIX)
            cnt += 1
            if self._sandbox_meta.get(sandbox_id) is not None:
                try:
                    image = self._sandbox_meta[sandbox_id]["image"]
                except Exception:
                    image = "default"
                meta[sandbox_id] = {"image": image}
        return cnt, meta

    def stop_monitoring(self):
        if self.scheduler and self.scheduler.running:
            logger.info("Stopping APScheduler...")
            self.scheduler.shutdown(wait=True)
            logger.info("APScheduler stopped")

    def __del__(self):
        """Destructor, ensure resource cleanup"""
        try:
            self.stop_monitoring()
        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}")
            pass
