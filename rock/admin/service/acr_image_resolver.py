"""Opt-in image registry mapping based on the responding ACR instance."""

import asyncio
from time import monotonic

import httpx
from requests.utils import parse_dict_header

from rock.logger import init_logger
from rock.utils.docker import ImageUtil

logger = init_logger(__name__)


class AcrRegionImageResolver:
    def __init__(
        self,
        client: httpx.AsyncClient,
        source_registries: list[str] | None = None,
        region_registry_mapping: dict[str, str] | None = None,
        timeout_seconds: float = 1.0,
        cache_ttl_seconds: float = 300.0,
        failure_cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self._source_registries = frozenset(source_registries or [])
        self._region_registry_mapping = dict(region_registry_mapping or {})
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._failure_cache_ttl_seconds = failure_cache_ttl_seconds
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._locks = {registry: asyncio.Lock() for registry in self._source_registries}

    async def resolve(self, image: str) -> str:
        registry, repository = ImageUtil.parse_registry_and_others(image)
        if registry not in self._source_registries or not self._region_registry_mapping:
            return image

        cached = self._cache.get(registry)
        if cached is not None and cached[1] > monotonic():
            target = cached[0]
        else:
            try:
                target = await asyncio.wait_for(self._resolve_target(registry), timeout=self._timeout_seconds)
            except asyncio.TimeoutError:
                return image
        return f"{target}/{repository}" if target else image

    async def _resolve_target(self, registry: str) -> str | None:
        async with self._locks[registry]:
            cached = self._cache.get(registry)
            if cached is not None and cached[1] > monotonic():
                return cached[0]

            target = None
            try:
                region = await self._probe_region(registry)
                target = self._region_registry_mapping.get(region)
            except Exception as error:
                logger.warning("ACR region probe failed for %s (%s)", registry, type(error).__name__)
            finally:
                # Cancellation also leaves a short negative cache entry, so a
                # timed-out leader does not trigger a new probe for each waiter.
                ttl = self._cache_ttl_seconds if target else self._failure_cache_ttl_seconds
                self._cache[registry] = (target, monotonic() + ttl)
            return target

    async def _probe_region(self, registry: str) -> str | None:
        async with self._client.stream(
            "GET",
            f"https://{registry}/v2/",
            auth=None,
            follow_redirects=False,
            timeout=self._timeout_seconds,
        ) as response:
            if response.status_code not in (200, 401):
                return None
            challenge = response.headers.get("www-authenticate", "")
            scheme, _, parameters = challenge.partition(" ")
            if scheme.lower() != "bearer":
                return None
            challenge_params = {key.strip().lower(): value for key, value in parse_dict_header(parameters).items()}
            service = (challenge_params.get("service") or "").strip().strip('"').split(":")
            if len(service) < 4 or service[0] != "registry.aliyuncs.com" or not service[3].startswith("cri-"):
                return None
            return service[1] or None
