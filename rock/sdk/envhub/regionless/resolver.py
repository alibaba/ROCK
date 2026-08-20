"""Rewrite container image references to a ROCK mirror registry when available.

Resolution rule:

- Read ``INSTANCE_ROCK_REGISTRY`` (one or more ``host/namespace`` entries
  separated by ``,`` or ``;``, e.g.
  ``reg-a.aliyuncs.com/mirror-1,reg-b.aliyuncs.com/mirror-2``). If unset/empty,
  return the original image unchanged.
- Take the *last* path segment of the image reference as the image name and
  combine it with the original tag/digest:
  ``swebench/sweb.eval.x86_64.foo:latest`` →
  ``<registry>/sweb.eval.x86_64.foo:latest``.
- For each configured registry in order, probe the candidate via the Docker
  Registry v2 manifest API (``GET /v2/{repo}/manifests/{tag}``), with Bearer
  token authentication support and a short timeout. Return the *first*
  candidate that exists; if none exist (or the probes time out / fail),
  fall back to the original image. Probe results are cached in-process so
  concurrent trials do not hammer the registry.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx

from rock.logger import init_logger
from rock.utils.registry import parse_image_ref, probe_manifest

logger = init_logger(__name__)

ROCK_REGISTRY_ENV = "INSTANCE_ROCK_REGISTRY"
_DEFAULT_PROBE_TIMEOUT_SEC = 5.0
_REGISTRY_SEPARATORS = (",", ";")

_FROM_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--\S+\s+)*)(?P<image>\S+)(?P<suffix>.*)$",
    re.IGNORECASE,
)


class RockRegistryResolver:
    """Resolves container image references to ROCK mirror registries.

    Args:
        registries: Explicit list of mirror registries (``host/namespace``).
            When *None* (default), falls back to reading the
            ``INSTANCE_ROCK_REGISTRY`` environment variable at resolve time.
    """

    def __init__(self, registries: list[str] | None = None) -> None:
        self._registries = registries
        self._resolve_cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    @staticmethod
    def _parse_registries(raw: str) -> list[str]:
        """Split the env value into an ordered list of non-empty registry entries."""
        if not raw:
            return []
        tokens = [raw]
        for sep in _REGISTRY_SEPARATORS:
            tokens = [piece for token in tokens for piece in token.split(sep)]
        return [token.strip().rstrip("/") for token in tokens if token.strip()]

    @staticmethod
    def _build_candidate(image: str, registry: str) -> str:
        """Build the candidate image reference under the ROCK registry.

        Strips the original registry and first-level namespace, preserving any
        nested namespaces and the image name plus tag/digest.
        Example: ``ghcr.io/foo/bar/baz:v1`` with registry ``reg/ns`` →
        ``reg/ns/bar/baz:v1``.
        """
        _, repo, tag = parse_image_ref(image)
        if "/" in repo:
            _, repo = repo.split("/", 1)
        return f"{registry.rstrip('/')}/{repo}:{tag}"

    @staticmethod
    def _build_candidate_with_original_namespace(image: str, registry: str) -> str:
        """Build candidate preserving the original namespace from the image.

        Replaces only the registry host, keeping the original namespace and
        image name intact.
        Example: ``ghcr.io/swebench/foo:v1`` with registry
        ``reg.aliyuncs.com/fixed-ns`` → ``reg.aliyuncs.com/swebench/foo:v1``.
        """
        reg_host, repo, tag = parse_image_ref(image)
        if not reg_host:
            # No registry host in image — repo is the full path
            pass
        mirror_host = registry.split("/", 1)[0] if "/" in registry else registry
        return f"{mirror_host}/{repo}:{tag}"

    async def _http_probe_manifest(self, image: str, timeout_sec: float) -> bool:
        """Check whether *image* exists on its registry via the v2 manifest API."""
        registry, repo, tag = parse_image_ref(image)
        if not registry or not repo:
            return False

        try:
            return await probe_manifest(registry=registry, repo=repo, tag=tag, timeout=timeout_sec)
        except httpx.HTTPError:
            logger.debug("HTTP probe for %s failed (network/protocol)", image, exc_info=True)
            return False
        except (ValueError, KeyError):
            logger.debug("HTTP probe for %s failed (response parsing)", image, exc_info=True)
            return False
        except Exception:
            logger.warning("HTTP probe for %s failed (unexpected)", image, exc_info=True)
            return False

    async def resolve_image(
        self,
        image: str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> str:
        """Return a ROCK-mirrored image reference if available, else ``image``."""
        if not image:
            return image

        if "@" in image:
            return image

        if self._registries is not None:
            registries = self._registries
        else:
            registries = self._parse_registries(os.environ.get(ROCK_REGISTRY_ENV, ""))
        if not registries:
            return image

        cache_key = f"{'|'.join(registries)}||{image}"
        async with self._cache_lock:
            cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        resolved = image
        for registry in registries:
            candidates = []
            original_ns = self._build_candidate_with_original_namespace(image, registry)
            fixed_ns = self._build_candidate(image, registry)
            if original_ns != image:
                candidates.append(original_ns)
            if fixed_ns != image and fixed_ns not in candidates:
                candidates.append(fixed_ns)

            found = False
            for candidate in candidates:
                if await self._http_probe_manifest(candidate, timeout_sec=timeout_sec):
                    logger.info("Rewriting image %s -> %s (ROCK mirror)", image, candidate)
                    resolved = candidate
                    found = True
                    break
            if found:
                break

        async with self._cache_lock:
            self._resolve_cache[cache_key] = resolved
        return resolved

    async def resolve_dockerfile(
        self,
        dockerfile: Path,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> bool:
        """Rewrite ``FROM`` images in *dockerfile* to ROCK mirrors when available.

        Returns True if any ``FROM`` line was rewritten.
        """
        text = dockerfile.read_text()
        lines = text.splitlines(keepends=True)
        changed = False

        for i, line in enumerate(lines):
            m = _FROM_RE.match(line.rstrip("\n\r"))
            if not m:
                continue
            original = m.group("image")
            resolved = await self.resolve_image(original, timeout_sec=timeout_sec)
            if resolved != original:
                eol = line[len(line.rstrip("\n\r")) :]
                lines[i] = f"{m.group('prefix')}{resolved}{m.group('suffix')}{eol}"
                changed = True

        if changed:
            dockerfile.write_text("".join(lines))
        return changed

    def reset_cache(self) -> None:
        """Clear the in-process resolve cache. Test helper."""
        self._resolve_cache.clear()


RegionlessResolver = RockRegistryResolver

_default_resolver = RockRegistryResolver()

resolve_image = _default_resolver.resolve_image
resolve_dockerfile = _default_resolver.resolve_dockerfile
