import ray
import time

from rock.config import RayConfig
from rock.logger import init_logger
from rock.utils.rwlock import AsyncRWLock
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger


logger = init_logger(__name__)


class RayService:
    def __init__(self, config: RayConfig):
        self._config = config
        self._ray_rwlock = AsyncRWLock()
        self._ray_reqeust_count = 0
        self._ray_establish_time = time.time()

    def init(self):
        ray.init(
            address=self._config.address,
            runtime_env=self._config.runtime_env,
            namespace=self._config.namespace,
            resources=self._config.resources,
        )
        if self._config.ray_reconnect_enabled:
            self._setup_ray_reconnect_scheduler()
        logger.info("end to init ray")

    def increment_ray_request_count(self):
        self._ray_reqeust_count += 1

    def get_ray_rwlock(self):
        return self._ray_rwlock
    

    def _setup_ray_reconnect_scheduler(self):
        self._ray_reconnection_scheduler = AsyncIOScheduler(
            timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
        )
        self._ray_reconnection_scheduler.add_job(
            func=self._ray_reconnect_with_policy,
            trigger=IntervalTrigger(seconds=self._config.ray_reconnect_check_interval_seconds),
            id="ray_reconnection",
            name="Ray Reconnect",
        )
        self._ray_reconnection_scheduler.start()

    async def _ray_reconnect_with_policy(self):
        if self._ray_request_count > self._config.ray_reconnect_request_threshold:
            await self._reconnect_ray()
        else:
            ray_connecting_time = time.time() - self._ray_establish_time
            if ray_connecting_time > self._config.ray_reconnect_interval_seconds:
                await self._reconnect_ray()

    async def _reconnect_ray(self):
        async with self._ray_rwlock.write_lock():
            start_time = time.time()
            logger.info(f"current time {start_time}, Reconnect ray cluster")
            ray.shutdown()
            ray.init(
                address=self._config.address,
                runtime_env=self._config.runtime_env,
                namespace=self._config.namespace,
                resources=self._config.resources,
            )
            self._ray_request_count = 0
            end_time = time.time()
            self._ray_establish_time = end_time
            logger.info(
                f"current time {end_time}, Reconnect ray cluster successfully, duration {end_time - start_time}s"
            )
