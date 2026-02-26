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
        self._api_client = api_client
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
        
        resource_version = resources.get('metadata', {}).get('resourceVersion')
        async with self._cache_lock:
            self._cache.clear()
            for item in resources.get('items', []):
                name = item.get('metadata', {}).get('name')
                if name:
                    self._cache[name] = item
        return resource_version
    
    async def _watch_resources(self):
        """Background task to watch resources and maintain cache.
        
        Implements Kubernetes Informer pattern:
        1. Initial list-and-sync to populate cache
        2. Continuous watch for ADDED/MODIFIED/DELETED events
        3. Auto-reconnect on watch timeout or network failures
        4. Re-sync on reconnect to avoid event loss
        """
        resource_version = None
        try:
            resource_version = await self._list_and_sync_cache()
            logger.info(f"Initial cache populated with {len(self._cache)} resources, resourceVersion={resource_version}")
        except Exception as e:
            logger.error(f"Failed to populate initial cache: {e}")
        
        while True:
            try:
                def _watch_in_thread():
                    w = watch.Watch()
                    stream = w.stream(
                        self._custom_api.list_namespaced_custom_object,
                        group=self._group,
                        version=self._version,
                        namespace=self._namespace,
                        plural=self._plural,
                        resource_version=resource_version,
                        timeout_seconds=self._watch_timeout_seconds,
                    )
                    events = []
                    for event in stream:
                        events.append(event)
                    return events
                
                events = await asyncio.to_thread(_watch_in_thread)
                
                async with self._cache_lock:
                    for event in events:
                        event_type = event['type']
                        obj = event['object']
                        name = obj.get('metadata', {}).get('name')
                        new_rv = obj.get('metadata', {}).get('resourceVersion')
                        
                        if new_rv:
                            resource_version = new_rv
                        
                        if not name:
                            continue
                        
                        if event_type in ['ADDED', 'MODIFIED']:
                            self._cache[name] = obj
                        elif event_type == 'DELETED':
                            self._cache.pop(name, None)
            
            except asyncio.CancelledError:
                logger.info("Watch task cancelled")
                raise
            except Exception as e:
                logger.warning(f"Watch stream disconnected: {e}, reconnecting immediately...")
                try:
                    resource_version = await self._list_and_sync_cache()
                    logger.info(f"Re-synced cache with {len(self._cache)} resources, resourceVersion={resource_version}")
                except Exception as list_err:
                    logger.error(f"Failed to re-list resources: {list_err}, retrying in {self._watch_reconnect_delay_seconds}s...")
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
