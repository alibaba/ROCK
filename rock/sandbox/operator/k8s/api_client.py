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
from collections.abc import Callable
from typing import Any

from aiolimiter import AsyncLimiter
from kubernetes import client

from rock.logger import init_logger
from rock.utils.k8s.informer import SharedInformer
from rock.utils.k8s.informer.cache import ObjectCache

logger = init_logger(__name__)

# User-Agent for K8s API requests
USER_AGENT = "rock-k8s-client/v1.0.0"


def _make_list_func(
    custom_api: client.CustomObjectsApi,
    group: str,
    version: str,
    plural: str,
) -> Callable:
    """Create a list function compatible with SharedInformer.

    SharedInformer expects a callable that accepts optional keyword arguments:
    - namespace: str (the namespace to list from)
    - watch: bool (for watch mode)
    - resource_version: str (for resuming watch)
    - timeout_seconds: int (watch timeout)
    - label_selector: str
    - field_selector: str

    Returns a function that matches this signature.
    """

    def list_func(
        namespace: str,
        watch: bool = False,
        resource_version: str | None = None,
        timeout_seconds: int | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        **kwargs,
    ):
        # CustomObjectsApi returns dict (unlike CoreV1Api which returns model objects)
        return custom_api.list_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            watch=watch,
            resource_version=resource_version,
            timeout_seconds=timeout_seconds,
            label_selector=label_selector,
            field_selector=field_selector,
            **kwargs,
        )

    return list_func


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
        resync_period: int = 0,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ):
        """Initialize K8s API client.

        Args:
            api_client: Kubernetes ApiClient instance
            group: CRD API group
            version: CRD API version
            plural: CRD resource plural name
            namespace: Namespace for operations
            qps: Queries per second limit (default: 5 for small clusters)
            resync_period: How often (seconds) to perform a full re-list.
                Defaults to 0 which disables periodic resyncs.
            label_selector: Optional label selector for filtering resources
            field_selector: Optional field selector for filtering resources
        """
        self._group = group
        self._version = version
        self._plural = plural
        self._namespace = namespace

        # Set custom User-Agent for identification
        api_client.user_agent = USER_AGENT

        self._custom_api = client.CustomObjectsApi(api_client)

        # Rate limiting
        self._rate_limiter = AsyncLimiter(max_rate=qps, time_period=1.0)

        # Create SharedInformer with custom list function
        list_func = _make_list_func(self._custom_api, group, version, plural)
        self._informer = SharedInformer(
            list_func=list_func,
            namespace=namespace,
            resync_period=resync_period,
            label_selector=label_selector,
            field_selector=field_selector,
        )
        self._initialized = False

    @property
    def cache(self) -> ObjectCache:
        """Access the underlying ObjectCache."""
        return self._informer.cache

    async def start(self) -> None:
        """Start the API client and initialize cache watch."""
        if self._initialized:
            return
        self._informer.start()
        self._initialized = True
        logger.info(f"Started K8sApiClient watch for {self._plural} in namespace {self._namespace}")

    async def stop(self) -> None:
        """Stop the informer and clean up resources."""
        if self._initialized:
            self._informer.stop()
            self._initialized = False
            logger.info(f"Stopped K8sApiClient watch for {self._plural} in namespace {self._namespace}")

    async def list_custom_objects(self) -> list[dict[str, Any]]:
        """List all cached custom resources.

        Returns:
            List of all cached resources
        """
        # ObjectCache.list() is thread-safe, no need for async wrapper
        return self._informer.cache.list()

    async def get_custom_object(self, name: str) -> dict[str, Any] | None:
        """Get a custom resource from cache.

        Args:
            name: Resource name

        Returns:
            Resource object or None if not found
        """
        # For namespaced resources, the key is namespace/name
        key = f"{self._namespace}/{name}"
        resource = self._informer.cache.get_by_key(key)
        if resource:
            return resource

        # Cache miss - query API Server directly
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
        return resource

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

    async def update_custom_object(
        self,
        name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a custom resource.

        Args:
            name: Resource name
            body: Updated resource manifest

        Returns:
            Updated resource
        """
        async with self._rate_limiter:
            return await asyncio.to_thread(
                self._custom_api.patch_namespaced_custom_object,
                group=self._group,
                version=self._version,
                namespace=self._namespace,
                plural=self._plural,
                name=name,
                body=body,
            )

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
