import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import ray
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from rock import InternalServerRockError
from rock.config import RayConfig
from rock.logger import init_logger
from rock.utils.rwlock import AsyncRWLock

logger = init_logger(__name__)


class RayService:
    def __init__(self, config: RayConfig, executor: ThreadPoolExecutor = None):
        self._config = config
        self._ray_rwlock = AsyncRWLock()
        self._ray_reqeust_count = 0
        self._ray_establish_time = time.time()
        self._executor = executor or ThreadPoolExecutor(max_workers=10)

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
        try:
            async with self._ray_rwlock.write_lock(timeout=self._config.ray_reconnect_wait_timeout_seconds):
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
        except InternalServerRockError as e:
            logger.warning("Reconnect ray cluster timeout, skip reconnectting", exc_info=e)

    async def async_ray_get(self, ray_future: ray.ObjectRef, timeout: int = 60):
        """
        Asynchronously get the result of a Ray ObjectRef.
        
        Args:
            ray_future: Ray ObjectRef to get result from
            timeout: Timeout in seconds for the ray.get operation
            
        Returns:
            The result from the Ray ObjectRef
            
        Raises:
            Exception: If ray.get fails
        """
        self.increment_ray_request_count()
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, lambda r: ray.get(r, timeout=timeout), ray_future)
        except Exception as e:
            logger.error("ray get failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get failed, {str(e)}"
            raise Exception(error_msg)
        return result

    async def async_ray_get_actor(self, actor_name: str, namespace: str = None):
        """
        Asynchronously get a Ray actor by name.
        
        Args:
            actor_name: Name of the actor to retrieve
            namespace: Ray namespace (optional, uses config namespace if not provided)
            
        Returns:
            The Ray actor handle
            
        Raises:
            ValueError: If actor does not exist
            Exception: If ray.get_actor fails
        """
        self.increment_ray_request_count()
        namespace = namespace or self._config.namespace
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor, ray.get_actor, actor_name, namespace
            )
        except ValueError as e:
            logger.error(f"ray get actor, actor {actor_name} not exist", exc_info=e)
            raise e
        except Exception as e:
            logger.error("ray get actor failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get actor failed, {str(e)}"
            raise Exception(error_msg)
        return result
