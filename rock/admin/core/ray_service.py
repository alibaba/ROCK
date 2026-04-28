import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import ray
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from rock import InternalServerRockError
from rock._codes import codes
from rock.config import RayConfig
from rock.logger import init_logger
from rock.utils.rwlock import AsyncRWLock

logger = init_logger(__name__)


class RayService:
    def __init__(self, config: RayConfig, executor: ThreadPoolExecutor = None):
        self._config = config
        self._ray_rwlock = AsyncRWLock()
        self._ray_request_count = 0
        self._ray_establish_time = time.time()
        self._executor = executor or ThreadPoolExecutor(max_workers=10)

    def init(self):
        ray.init(
            address=self._config.address,
            runtime_env=self._config.runtime_env,
            namespace=self._config.namespace,
            resources=self._config.resources,
            _temp_dir=self._config.temp_dir,
        )
        if self._config.ray_reconnect_enabled:
            self._setup_ray_reconnect_scheduler()
        logger.info("end to init ray")

    def increment_ray_request_count(self):
        self._ray_request_count += 1

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
        logger.info("APScheduler started for reconnecting ray cluster.")

    async def _ray_reconnect_with_policy(self):
        if self._ray_request_count > self._config.ray_reconnect_request_threshold:
            await self._reconnect_ray()
            return
        else:
            ray_connecting_time = time.time() - self._ray_establish_time
            if ray_connecting_time > self._config.ray_reconnect_interval_seconds:
                await self._reconnect_ray()
                return
        logger.info(
            f"Skip reconnecting ray cluster, current ray request count {self._ray_request_count}, ray connecting time {ray_connecting_time}s"
        )

    async def _reconnect_ray(self):
        try:
            async with self._ray_rwlock.write_lock(timeout=self._config.ray_reconnect_wait_timeout_seconds):
                max_attempts = max(1, self._config.ray_reconnect_max_attempts)
                backoff = self._config.ray_reconnect_retry_backoff_seconds
                last_exc: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    start_time = time.time()
                    logger.info(f"current time {start_time}, Reconnect ray cluster (attempt {attempt}/{max_attempts})")
                    try:
                        ray.shutdown()
                        ray.init(
                            address=self._config.address,
                            runtime_env=self._config.runtime_env,
                            namespace=self._config.namespace,
                            resources=self._config.resources,
                            _temp_dir=self._config.temp_dir,
                        )
                    except Exception as e:
                        last_exc = e
                        logger.warning(
                            f"Reconnect ray cluster attempt {attempt}/{max_attempts} failed: {e}", exc_info=e
                        )
                        if attempt < max_attempts and backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    self._ray_request_count = 0
                    end_time = time.time()
                    self._ray_establish_time = end_time
                    logger.info(
                        f"current time {end_time}, Reconnect ray cluster successfully, "
                        f"duration {end_time - start_time}s"
                    )
                    return
                logger.critical(
                    f"Ray reconnect failed after {max_attempts} attempts; ray cluster is in shutdown state. "
                    f"Last error: {last_exc}",
                    exc_info=last_exc,
                )
        except InternalServerRockError as e:
            logger.warning("Reconnect ray cluster timeout, skip reconnectting", exc_info=e)

    def _ensure_ray_initialized(self) -> None:
        """Reject the call if Ray is not initialized.

        Why: a failed periodic ``_reconnect_ray`` may leave the process in a
        ``ray.shutdown`` state. Without this guard a subsequent ``ray.get`` /
        ``ray.get_actor`` would fall through to Ray's auto-init hook and
        ``ray.init()`` with no args, spawning a local cluster on the admin host
        and OOM-ing under concurrent requests.
        """
        if not ray.is_initialized():
            raise InternalServerRockError(
                "Ray cluster is not initialized; refusing to call ray API to avoid "
                "spawning a local cluster via auto-init.",
                code=codes.INTERNAL_SERVER_ERROR,
            )

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
        self._ensure_ray_initialized()
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
        self._ensure_ray_initialized()
        self.increment_ray_request_count()
        namespace = namespace or self._config.namespace
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, ray.get_actor, actor_name, namespace)
        except ValueError as e:
            logger.error(f"ray get actor, actor {actor_name} not exist", exc_info=e)
            raise e
        except Exception as e:
            logger.error("ray get actor failed", exc_info=e)
            error_msg = str(e.args[0]) if len(e.args) > 0 else f"ray get actor failed, {str(e)}"
            raise Exception(error_msg)
        return result
