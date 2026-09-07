"""Opt-in image registry mapping based on the responding ACR instance."""

import asyncio
from time import monotonic

import httpx
from requests.utils import parse_dict_header

from rock.admin.core.redis_key import dadi_image_cache_key
from rock.common.constants import (
    DADI_IMAGE_REDIS_CACHE_TTL_SECONDS,
    DADI_IMAGE_REDIS_TIMEOUT_SECONDS,
    DADI_IMAGE_TAG_SUFFIXES,
    REGISTRY_MANIFEST_ACCEPT,
)
from rock.logger import init_logger
from rock.utils.docker import ImageUtil
from rock.utils.providers.redis_provider import RedisProvider

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
        probe_registry: str | None = None,
        dadi_tag_suffixes: list[str] | tuple[str, ...] | None = DADI_IMAGE_TAG_SUFFIXES,
        redis_provider: RedisProvider | None = None,
        dadi_redis_cache_ttl_seconds: int = DADI_IMAGE_REDIS_CACHE_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._source_registries = frozenset(source_registries or [])
        self._probe_registry = probe_registry
        self._region_registry_mapping = dict(region_registry_mapping or {})
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._failure_cache_ttl_seconds = failure_cache_ttl_seconds
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._locks = {
            registry: asyncio.Lock() for registry in {probe_registry or source for source in self._source_registries}
        }
        self._dadi_tag_suffixes = tuple(dadi_tag_suffixes or ())
        self._redis_provider = redis_provider
        self._dadi_redis_cache_ttl_seconds = dadi_redis_cache_ttl_seconds

    async def resolve(self, image: str) -> str:
        registry, repository = ImageUtil.parse_registry_and_others(image)
        if registry not in self._source_registries or not self._region_registry_mapping:
            return image

        registry = self._probe_registry or registry
        cached = self._cache.get(registry)
        if cached is not None and cached[1] > monotonic():
            target = cached[0]
        else:
            try:
                target = await asyncio.wait_for(self._resolve_target(registry), timeout=self._timeout_seconds)
            except asyncio.TimeoutError:
                return image
        if not target:
            return image
        mapped_image = f"{target}/{repository}"
        return await self._resolve_dadi_image(mapped_image, probe_registry=registry)

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

    async def _resolve_dadi_image(self, image: str, *, probe_registry: str) -> str:
        registry, reference = ImageUtil.parse_registry_and_others(image)
        if not self._dadi_tag_suffixes or "@" in reference:
            return image
        repository, separator, tag = reference.rpartition(":")
        if not separator:
            repository, tag = reference, ImageUtil.DEFAULT_TAG
        if not tag or tag.endswith(("_accelerated", *self._dadi_tag_suffixes)):
            return image
        for suffix in self._dadi_tag_suffixes:
            candidate_tag = tag + suffix
            if len(candidate_tag) > 128:
                continue
            candidate = f"{registry}/{repository}:{candidate_tag}"
            key = (probe_registry, candidate)
            if await self._get_dadi_redis_cache(key):
                return candidate
            try:
                exists = await asyncio.wait_for(
                    self._dadi_manifest_exists(probe_registry, repository, candidate_tag),
                    timeout=self._timeout_seconds,
                )
            except Exception as error:
                logger.warning("DADI image probe failed for %s (%s)", probe_registry, type(error).__name__)
                continue
            if exists:
                # Persistence has its own timeout; it cannot discard a confirmed manifest.
                await self._set_dadi_redis_cache(key)
                return candidate
        return image

    async def _get_dadi_redis_cache(self, key: tuple[str, str]) -> bool:
        if self._redis_provider is None:
            return False
        try:
            cached = await asyncio.wait_for(
                self._redis_provider.json_get(dadi_image_cache_key(*key), path="."),
                timeout=DADI_IMAGE_REDIS_TIMEOUT_SECONDS,
            )
            return cached is True
        except Exception as error:
            logger.warning("DADI Redis cache read failed (%s)", type(error).__name__)
            return False

    async def _set_dadi_redis_cache(self, key: tuple[str, str]) -> None:
        if self._redis_provider is None:
            return
        try:
            await asyncio.wait_for(
                self._redis_provider.json_set_with_ttl(
                    dadi_image_cache_key(*key), "$", True, self._dadi_redis_cache_ttl_seconds
                ),
                timeout=DADI_IMAGE_REDIS_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning("DADI Redis cache write failed (%s)", type(error).__name__)

    async def _dadi_manifest_exists(self, registry: str, repository: str, tag: str) -> bool:
        origin = httpx.URL(f"https://{registry}")
        manifest_url = f"{origin}/v2/{repository}/manifests/{tag}"
        options = {"auth": None, "follow_redirects": False, "timeout": self._timeout_seconds}
        headers = {"Accept": REGISTRY_MANIFEST_ACCEPT}
        response = await self._client.head(manifest_url, headers=headers, **options)
        if response.status_code != 401:
            return response.status_code == 200

        scheme, _, parameters = response.headers.get("www-authenticate", "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        challenge = {key.strip().lower(): value for key, value in parse_dict_header(parameters).items()}
        realm = httpx.URL(challenge.get("realm") or "")
        # Only ask the responding registry for an anonymous, pull-scoped token.
        if (
            realm.scheme != "https"
            or realm.host != origin.host
            or realm.port != origin.port
            or realm.username
            or realm.password
            or not challenge.get("service")
        ):
            return False
        response = await self._client.get(
            realm,
            params={"service": challenge["service"], "scope": f"repository:{repository}:pull"},
            headers={"Accept": "application/json"},
            **options,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            return False
        response = await self._client.head(
            manifest_url, headers={**headers, "Authorization": f"Bearer {token}"}, **options
        )
        return response.status_code == 200
