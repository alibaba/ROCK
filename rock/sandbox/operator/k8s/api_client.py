"""K8s API Client with rate limiting and local cache.

This module provides a wrapper around Kubernetes CustomObjectsApi with:
- Rate limiting using aiolimiter (configurable QPS)
- Local cache with watch-based sync (Informer pattern)
- Consistent error handling
- Simple CRUD interface for K8s CR operations

The rate limiter uses token bucket algorithm to prevent API Server overload.
The Informer pattern reduces API Server load by maintaining a local cache.
"""

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from aiolimiter import AsyncLimiter
from kubernetes import client, watch

from rock.logger import init_logger

logger = init_logger(__name__)


class K8sApiClient:
    """K8s API client wrapper with rate limiting and Informer cache.

    Centralizes K8s API Server access with:
    - Rate limiting via aiolimiter to prevent API Server overload
    - Local cache with watch-based sync (Informer pattern) to reduce API calls
    - Consistent error handling
    - Simple CRUD interface for K8s custom resources
    """

    def __init__(
        self,
        api_client: client.ApiClient,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        qps: float = 5.0,
        watch_timeout_seconds: int = 60,
        watch_reconnect_delay_seconds: int = 5,
    ):
        """Initialize K8s API client.

        Args:
            api_client: Kubernetes ApiClient instance
            group: CRD API group
            version: CRD API version
            plural: CRD resource plural name
            namespace: Namespace for operations
            qps: Queries per second limit (default: 5 for small clusters)
            watch_timeout_seconds: Watch timeout before reconnect (default: 60)
            watch_reconnect_delay_seconds: Delay after watch failure (default: 5)
        """
        self._group = group
        self._version = version
        self._plural = plural
        self._namespace = namespace
        self._custom_api = client.CustomObjectsApi(api_client)

        # Rate limiting
        self._rate_limiter = AsyncLimiter(max_rate=qps, time_period=1.0)

        # Watch configuration
        self._watch_timeout_seconds = watch_timeout_seconds
        self._watch_reconnect_delay_seconds = watch_reconnect_delay_seconds

        # Local cache for resources (Informer pattern)
        self._cache: dict[str, dict] = {}
        self._cache_lock = asyncio.Lock()
        self._watch_task = None
        self._initialized = False
        self._event_queue: asyncio.Queue | None = None
        self._watch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="k8s-watch-")
        self._stop_event = threading.Event()
        self._resource_version: str | None = None
        self._resource_version_lock = threading.Lock()

    async def start(self):
        """Start the API client and initialize cache watch."""
        if self._initialized:
            return

        self._watch_task = asyncio.create_task(self._watch_resources())
        self._initialized = True
        logger.info(f"Started K8sApiClient watch for {self._plural} in namespace {self._namespace}")

    async def _list_and_sync_cache(self) -> str:
        """List all resources and sync to cache.

        Returns:
            resourceVersion for next watch
        """
        async with self._rate_limiter:
            resources = await asyncio.to_thread(
                self._custom_api.list_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
            )

        resource_version = resources.get("metadata", {}).get("resourceVersion")
        async with self._cache_lock:
            self._cache.clear()
            for item in resources.get("items", []):
                name = item.get("metadata", {}).get("name")
                if name:
                    self._cache[name] = item
        self._advance_resource_version(resource_version)
        return self._resource_version

    def _watch_in_thread(self, resource_version: str | None, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        """Watch resources in a background thread and put events to queue.

        Args:
            resource_version: The resource version to watch from
            event_queue: Queue to put events for async processing
            loop: The asyncio event loop to use for queue operations
        """
        try:
            w = watch.Watch()
            for event in w.stream(
                self._custom_api.list_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
                resource_version=resource_version,
                timeout_seconds=self._watch_timeout_seconds,
            ):
                if self._stop_event.is_set():
                    break
                # Put event to queue for async processing
                asyncio.run_coroutine_threadsafe(event_queue.put(("event", event)), loop)
        except Exception as e:
            logger.warning(f"Watch thread error: {e}")
        finally:
            # Signal thread exit
            asyncio.run_coroutine_threadsafe(event_queue.put(("exit", None)), loop)

    def _advance_resource_version(self, rv: str | None) -> bool:
        """Advance resource_version only when rv is strictly newer.

        K8s resourceVersions are opaque strings but etcd encodes them as
        monotonically increasing integers. Prevents stale responses from
        rolling back the watch cursor.

        Returns:
            True if version was updated, False if skipped (old version or error)
        """
        if not rv:
            return False
        with self._resource_version_lock:
            if self._resource_version is None:
                self._resource_version = rv
                return True
            try:
                if int(rv) > int(self._resource_version):
                    self._resource_version = rv
                    return True
                return False
            except ValueError:
                # Non-integer resourceVersion — skip to avoid downgrade
                logger.error(f"Non-integer resourceVersion detected: rv={rv}, current={self._resource_version}, skipping")
                return False

    async def _process_events(self, event_queue: asyncio.Queue, thread_future: Future) -> str | None:
        """Process events from queue and update cache.

        Args:
            event_queue: Queue containing events from watch thread
            thread_future: Future representing the watch thread

        Returns:
            Latest resource version or None if disconnected
        """
        while True:
            try:
                msg_type, event = await asyncio.wait_for(event_queue.get(), timeout=1.0)

                if msg_type == "exit":  # Watch thread exited
                    break

                if msg_type == "event":
                    event_type = event["type"]
                    obj = event["object"]
                    name = obj.get("metadata", {}).get("name")
                    new_rv = obj.get("metadata", {}).get("resourceVersion")

                    # Skip event if no resourceVersion or version is stale
                    if not new_rv:
                        continue
                    if not self._advance_resource_version(new_rv):
                        continue

                    if not name:
                        continue

                    async with self._cache_lock:
                        if event_type in ["ADDED", "MODIFIED"]:
                            self._cache[name] = obj
                        elif event_type == "DELETED":
                            self._cache.pop(name, None)

                    logger.debug(f"Cache updated: {event_type} {name}, rv={self._resource_version}")

            except asyncio.TimeoutError:
                # Check if thread has exited
                if thread_future.done() and event_queue.empty():
                    break
                continue
            except asyncio.CancelledError:
                raise

        return self._resource_version

    async def _watch_resources(self):
        """Background task to watch resources and maintain cache.

        Implements Kubernetes Informer pattern:
        1. Initial list-and-sync to populate cache
        2. Continuous watch for ADDED/MODIFIED/DELETED events (real-time)
        3. Auto-reconnect on watch timeout or network failures
        4. Re-sync on reconnect to avoid event loss
        """
        try:
            await self._list_and_sync_cache()
            logger.info(
                f"Initial cache populated with {len(self._cache)} resources, resourceVersion={self._resource_version}"
            )
        except Exception as e:
            logger.error(f"Failed to populate initial cache: {e}")

        self._event_queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        while True:
            try:
                self._stop_event.clear()

                # Start watch in background thread
                thread_future = self._watch_executor.submit(
                    self._watch_in_thread,
                    self._resource_version,
                    self._event_queue,
                    loop
                )

                # Process events in real-time
                await self._process_events(self._event_queue, thread_future)

                # Wait for thread to complete
                if not thread_future.done():
                    self._stop_event.set()
                    try:
                        thread_future.result(timeout=5.0)
                    except Exception:
                        pass

            except asyncio.CancelledError:
                logger.info("Watch task cancelled")
                self._stop_event.set()
                raise
            except Exception as e:
                logger.warning(f"Watch stream disconnected: {e}, reconnecting immediately...")
                try:
                    await self._list_and_sync_cache()
                    logger.info(
                        f"Re-synced cache with {len(self._cache)} resources, resourceVersion={self._resource_version}"
                    )
                except Exception as list_err:
                    logger.error(
                        f"Failed to re-list resources: {list_err}, retrying in {self._watch_reconnect_delay_seconds}s..."
                    )
                    await asyncio.sleep(self._watch_reconnect_delay_seconds)

    async def create_custom_object(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a custom resource.

        Args:
            body: Resource manifest

        Returns:
            Created resource
        """
        async with self._rate_limiter:
            return await asyncio.to_thread(
                self._custom_api.create_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
                body=body,
            )

    async def get_custom_object(
        self,
        name: str,
    ) -> dict[str, Any]:
        """Get a custom resource (from cache with fallback to API Server).

        Args:
            name: Resource name

        Returns:
            Resource object
        """
        async with self._cache_lock:
            resource = self._cache.get(name)

        if resource:
            return resource

        logger.debug(f"Cache miss for {name}, querying API Server")
        async with self._rate_limiter:
            resource = await asyncio.to_thread(
                self._custom_api.get_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
                name=name,
            )

        async with self._cache_lock:
            self._cache[name] = resource

        return resource

    async def delete_custom_object(
        self,
        name: str,
    ) -> dict[str, Any]:
        """Delete a custom resource.

        Args:
            name: Resource name

        Returns:
            Delete status
        """
        async with self._rate_limiter:
            return await asyncio.to_thread(
                self._custom_api.delete_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
                name=name,
            )
